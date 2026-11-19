from __future__ import annotations
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from .accounting import calculate
from .models import AgentRun, OrderItem

TOOLS={
 "get_profit_summary":"查询所选时间的收入、成本、费用、贡献利润和待核对数量。",
 "get_order_details":"查询所选时间的脱敏订单明细。",
 "get_refund_summary":"按匿名商品汇总退款和订单数量。",
}
class AgentProblem(ValueError): pass

def yuan(fen):
    if fen is None: return None
    value=abs(fen)
    return f"{'-' if fen < 0 else ''}{value//100}.{value%100:02d}"

def period_from_question(q: str, start: str, end: str) -> tuple[str,str]:
    today=datetime.now(timezone(timedelta(hours=8))).date()
    if "上周" in q:
        monday=today-timedelta(days=today.weekday()+7);return monday.isoformat(),(monday+timedelta(days=6)).isoformat()
    if "上个月" in q or "上月" in q:
        last=today.replace(day=1)-timedelta(days=1);return last.replace(day=1).isoformat(),last.isoformat()
    if "本月" in q or "这个月" in q: return today.replace(day=1).isoformat(),today.isoformat()
    if "昨天" in q: yesterday=today-timedelta(days=1);return yesterday.isoformat(),yesterday.isoformat()
    if "今天" in q: return today.isoformat(),today.isoformat()
    if "近7天" in q or "最近7天" in q or "近一周" in q: return (today-timedelta(days=6)).isoformat(),today.isoformat()
    if "近30天" in q or "最近30天" in q: return (today-timedelta(days=29)).isoformat(),today.isoformat()
    found=re.findall(r"\b\d{4}-\d{2}-\d{2}\b",q)
    if len(found)==2: return found[0],found[1]
    return start,end

def canonical_intent(question: str) -> str:
    # Only a fixed intent is sent; the original question may contain sensitive data.
    refund=any(x in question for x in ("退款","退货","退单"))
    if any(x in question for x in ("退款率","退货率","退单率")):
        raise AgentProblem("暂不支持退款率排行，请改问退款笔数或退款金额")
    profit=any(x in question for x in ("利润","赚","收入","营业额","成本","费用","销售","亏","经营","有效订单"))
    detail=any(x in question for x in ("订单明细","哪一单","哪些订单","需要核对的订单"))
    payment=any(x in question for x in ("实付","支付金额","买家支付"))
    count=any(x in question for x in ("订单数","多少笔","几笔","几单")) and "订单" in question
    parts=[]
    if refund:
        if wants_refund_ranking(question):parts.append("只说明退款快照合计及退款笔数、金额的商品排行")
        else:parts.append("只说明成功退款订单数和退款快照合计")
    if profit:parts.append("只说明已核算订单的贡献利润及待核对情况")
    if payment:parts.append("只说明所选订单的实付快照")
    if detail:parts.append("只概括所选时间的订单明细和待核对项目")
    elif count and not refund:parts.append("只说明所选时间的付款订单数量")
    elif not parts and "订单" in question:parts.append("只概括所选时间的订单和待核对情况")
    if not parts:raise AgentProblem("请询问订单、退款、收入、成本或利润，并可指定上周、上月等时间")
    return "；".join(parts)

def normal_numbers(text: str) -> set:
    from decimal import Decimal, InvalidOperation
    # ISO date hyphens are separators, not negative signs.
    text=re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b",r"\1 \2 \3",text)
    tokens=re.findall(r"(?<![A-Za-z])[-+]?\d+(?:,\d{3})*(?:\.\d+)?",text)
    try: return {Decimal(x.replace(",","")) for x in tokens}
    except InvalidOperation: return set()

def wants_refund_ranking(question: str) -> bool:
    return any(x in question for x in ("最多","最高","最大","哪个","哪种","哪些商品","哪些规格"))

def refund_ranking_modes(question: str) -> tuple[bool, bool]:
    count=any(x in question for x in ("笔数","次数","多少笔","几笔","多少单","几单","订单数","单数"))
    amount=any(x in question for x in ("金额","退款额","退了多少元","退了多少钱","退款多少钱"))
    return (count or not amount, amount or not count)

def refund_ranking_data(items: list[dict], sku_aliases: dict[str,str]) -> dict:
    grouped={}
    for item in items:
        if item["refund_status"]!="success":continue
        row=grouped.setdefault(item["sku"],{"count":0,"amount":0,"missing":0})
        row["count"]+=1
        if item["refund_fen"] is None:row["missing"]+=1
        else:row["amount"]+=item["refund_fen"]
    def top(key):
        if not grouped:return None
        value=max(v[key] for v in grouped.values())
        if key=="amount" and value==0:return None
        names=sorted(sku_aliases.get(sku,sku) for sku,v in grouped.items() if v[key]==value)
        return {"value":value,"winners":names[:20],"winner_count":len(names)}
    return {"count":top("count"),"amount":top("amount"),"missing":sum(v["missing"] for v in grouped.values())}

def safe_data(report: dict):
    aliases={sku:f"商品{idx+1}" for idx,sku in enumerate(dict.fromkeys(x["sku"] for x in report["items"]))}
    k=report["known"];c=report["counts"]
    common={"开始日期":report["start"],"结束日期":report["end"],"最近导入":report["as_of"],"口径":"按支付日期，完成订单的已核算部分，费用仅包括已录入项目",
            "主订单数":c["paid_orders"],"子订单数":c["paid_items"],"已核算子订单数":c["settled"],"待核对数":c["needs_review"],"暂估数":c["pending"],"订单实付快照元":yuan(report["source_snapshot"]["payment_fen"]),"快照口径说明":"订单导出金额，仅供核对，不等同净收入",
            "净销售额元":yuan(k["net_sales_fen"]) if c["settled"] else None,"成本元":yuan(k["cost_fen"]) if c["settled"] else None,"费用元":yuan(k["fees_fen"]) if c["settled"] else None,"贡献利润元":yuan(k["profit_fen"]) if c["settled"] else None,"已核算订单退款元":yuan(k["refund_fen"]) if c["settled"] else None}
    grouped={}
    for item in report["items"]:
        key=aliases[item["sku"]];g=grouped.setdefault(key,{"商品":key,"子订单数":0,"退款子订单数":0,"退款元":0})
        g["子订单数"]+=1
        if item["refund_status"]=="success":
            g["退款子订单数"]+=1;g["退款元"]+=item["refund_fen"] or 0
    refunds=[]
    for rank,g in enumerate(sorted(grouped.values(),key=lambda v:(-v["退款元"],v["商品"])),1):
        row=dict(g);row["排名"]=rank;row["退款元"]=yuan(row["退款元"]);refunds.append(row)
    success_items=[x for x in report["items"] if x["refund_status"]=="success"]
    snapshot_summary={"成功退款子订单数":len(success_items),"退款快照合计元":yuan(sum(x["refund_fen"] or 0 for x in success_items)),
                      "说明":"金额来自订单导出快照，存在待核对订单时只能作为待核对金额"}
    def verified_refund_amount(item):
        if item["refund_review_id"] is not None:
            return item["reviewed_refund_fen"] or 0
        if item.get("refund_auto_verified"):
            return item["refund_fen"] or 0
        return None
    checked_success=[x for x in report["items"] if verified_refund_amount(x) is not None and x["refund_status"]=="success"]
    snapshot_summary["成功退款历史已核对子订单数"]=len(checked_success)
    snapshot_summary["成功退款历史已核对累计金额元"]=yuan(sum(verified_refund_amount(x) for x in checked_success))
    reviewed=[x for x in report["items"] if x["refund_review_id"] is not None and x["refund_status"]!="success"]
    snapshot_summary["其他订单人工核对子订单数"]=len(reviewed)
    snapshot_summary["其他订单人工核对成功退款元"]=yuan(sum(x["reviewed_refund_fen"] for x in reviewed))
    ranks=refund_ranking_data(report["items"],aliases)
    ranking_evidence={"口径":"订单快照中标为退款成功的子订单；金额为已记录的退款快照，非退款率",
                      "笔数最高":ranks["count"],"金额最高":None if ranks["amount"] is None else {**ranks["amount"],"value":yuan(ranks["amount"]["value"])},
                      "成功退款缺少快照金额数":ranks["missing"],"并列商品最多显示":20}
    details=[{"序号":i+1,"商品":aliases[x["sku"]],"付款日期":x["paid_at"][:10],"数量":x["quantity"],"实付快照元":yuan(x["payment_fen"]),"退款快照元":yuan(x["refund_fen"]),"已核对累计退款元":yuan(verified_refund_amount(x)) if verified_refund_amount(x) is not None else None,"利润元":yuan(x["profit_fen"]),"需核对":bool(x["reasons"])} for i,x in enumerate(report["items"][:20])]
    return {"get_profit_summary":common,"get_order_details":{"范围":common,"条目":details,"条目上限":20},"get_refund_summary":{"范围":common,"退款快照":snapshot_summary,"商品退款":refunds[:100],"排行依据":ranking_evidence}}, {v:k for k,v in aliases.items()}

def verified_answer(report: dict, question: str, aliases: dict[str,str]) -> str:
    """Render business facts from the calculation; never display unverified model claims."""
    c=report["counts"];k=report["known"];parts=[]
    refund=any(x in question for x in ("退款","退货","退单"))
    profit=any(x in question for x in ("利润","赚","收入","营业额","成本","费用","销售","亏","经营","有效订单"))
    detail=any(x in question for x in ("订单明细","哪一单","哪些订单","需要核对的订单"))
    count=any(x in question for x in ("订单数","多少笔","几笔","几单")) and "订单" in question
    if profit:
        if c["settled"]:
            revenue_requested=any(x in question for x in ("营业额","收入","销售"))
            valid_requested="有效订单" in question
            metrics=[]
            if any(x in question for x in ("利润","赚","亏")): metrics.append(f"纯利润 {yuan(k['profit_fen'])} 元")
            if revenue_requested: metrics.append(f"营业额 {yuan(report['source_snapshot']['payment_fen'])} 元（退款前实付快照）")
            if "成本" in question: metrics.append(f"商品成本 {yuan(k['cost_fen'])} 元")
            if "费用" in question: metrics.append(f"已录入费用 {yuan(k['fees_fen'])} 元")
            if valid_requested: metrics.append(f"有效订单 {c['settled']} 笔")
            if metrics:
                parts.append("；".join(metrics)+"。")
            else:
                parts.append(f"已核算 {c['settled']} 笔子订单，贡献利润为 {yuan(k['profit_fen'])} 元。")
        else:
            parts.append("当前范围没有可核算的订单，贡献利润暂无法确定。")
            if any(x in question for x in ("收入","销售")):
                parts.append(f"订单导出实付快照合计 {yuan(report['source_snapshot']['payment_fen'])} 元，仅供核对，不等同净收入。")
            if "营业额" in question:
                parts.append(f"营业额 {yuan(report['source_snapshot']['payment_fen'])} 元（退款前实付快照）。")
            if "有效订单" in question: parts.append("有效订单 0 笔。")
    if refund:
        success=[x for x in report["items"] if x["refund_status"]=="success"]
        missing=sum(x["refund_fen"] is None for x in success)
        total=sum(x["refund_fen"] or 0 for x in success)
        parts.append(f"订单快照中有 {len(success)} 笔子订单显示退款成功，已记录退款金额合计 {yuan(total)} 元。")
        def verified_refund_amount(item):
            if item["refund_review_id"] is not None:
                return item["reviewed_refund_fen"] or 0
            if item.get("refund_auto_verified"):
                return item["refund_fen"] or 0
            return None
        checked_success=[x for x in report["items"] if verified_refund_amount(x) is not None and x["refund_status"]=="success"]
        if checked_success:
            parts.append(f"其中 {len(checked_success)} 笔已自动核对完整退款历史，累计成功退款 {yuan(sum(verified_refund_amount(x) for x in checked_success))} 元；这些订单也包含在上述快照中。")
        reviewed=[x for x in report["items"] if x["refund_review_id"] is not None and x["refund_status"]!="success"]
        if reviewed:
            parts.append(f"另有 {len(reviewed)} 笔订单经人工核对，成功退款合计 {yuan(sum(x['reviewed_refund_fen'] for x in reviewed))} 元；与上述快照分开统计。")
        if missing:parts.append(f"另有 {missing} 笔成功退款缺少金额。")
        if wants_refund_ranking(question):
            ranks=refund_ranking_data(report["items"],aliases)
            show_count,show_amount=refund_ranking_modes(question)
            def names(rank):
                more=f"（共 {rank['winner_count']} 个并列，仅列前 20 个）" if rank['winner_count']>20 else ""
                return '、'.join(rank['winners'])+more
            if show_count and ranks["count"]:
                top=ranks["count"]
                parts.append(f"按订单快照中退款成功的子订单笔数，最多的是 {names(top)}，各 {top['value']} 笔。")
            if show_amount and ranks["amount"]:
                top=ranks["amount"]
                parts.append(f"按已记录的退款快照金额，最高的是 {names(top)}，各 {yuan(top['value'])} 元。")
            if show_amount and ranks["missing"]:
                parts.append("部分成功退款缺少快照金额，金额排行不代表完整退款总额。")
        if c["needs_review"]:parts.append("退款金额来自订单快照，存在待核对信息。")
    payment=any(x in question for x in ("实付","支付金额","买家支付"))
    if payment:
        if not report["items"]:parts.append("所选范围内没有已付款子订单，无法读取实付快照。")
        else:parts.append(f"所选子订单的实付快照合计为 {yuan(report['source_snapshot']['payment_fen'])} 元，退款口径仍以导入确认信息为准。")
    if detail or (count and not refund) or (not profit and not refund and "订单" in question):
        parts.append(f"按付款日期共有 {c['paid_orders']} 笔主订单，涉及 {c['paid_items']} 笔子订单，其中 {c['settled']} 笔子订单已核算。")
    if c["needs_review"]:parts.append(f"有 {c['needs_review']} 笔子订单需要核对，具体原因可在订单明细查看。")
    if c["pending"]:parts.append(f"未完成子订单共 {c['pending']} 笔，单列为暂估；可能与待核对子订单重叠。")
    return "\n".join(parts)

def call_model(messages, specs, base_url, key, model):
    parsed=urlsplit(base_url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme!="https" and not (parsed.scheme=="http" and parsed.hostname in ("localhost","127.0.0.1","::1"))):
        raise AgentProblem("模型地址必须为 HTTPS 或本机回环地址")
    try:
        with httpx.Client(timeout=httpx.Timeout(40,connect=5),follow_redirects=False,trust_env=False) as client:
            r=client.post(base_url.rstrip("/")+"/chat/completions",headers={"Authorization":"Bearer "+key},json={"model":model,"messages":messages,"tools":specs,"tool_choice":"auto","parallel_tool_calls":False,"stream":False})
            r.raise_for_status();data=r.json()
        message=data["choices"][0]["message"]
        if message.get("role")!="assistant": raise ValueError
        return message,data.get("usage",{})
    except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError): raise AgentProblem("模型服务暂不可用，请检查本机配置；核算功能不受影响") from None

def run(db: Session, store_id: int, start: str, end: str, question: str, *, sku_filter: str | None = None, model_call=call_model) -> dict:
    if not 1<=len(question.strip())<=500: raise AgentProblem("问题长度须为 1 到 500 字")
    intent=canonical_intent(question);start,end=period_from_question(question,start,end)
    order_numbers=list(dict.fromkeys(re.findall(r"(?<!\d)\d{12,25}(?!\d)",question))) if any(x in question for x in ("订单","单号")) else []
    if len(order_numbers)>1:raise AgentProblem("每次请只查询一个订单编号")
    selection={}
    if sku_filter:
        known=db.scalar(select(OrderItem.id).where(OrderItem.store_id==store_id,OrderItem.sku==sku_filter).limit(1))
        if known is None:raise AgentProblem("当前店铺没有该商品规格")
        selection["sku"]=sku_filter
    elif any(x in question for x in ("这个商品","该商品","此商品","这个规格","该规格","指定商品","商品ID","商品id")) or re.search(r"(?i)(?<![a-z])(?:chatgpt|claude|gemini|muse)(?![a-z])",question):
        raise AgentProblem("查询指定商品时，请先在页面选择商品规格")
    if order_numbers:
        identifier=order_numbers[0]
        item=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==identifier))
        parent=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.order_external_id==identifier))
        if item:selection["item_id"]=identifier
        elif parent:selection["order_id"]=identifier
        else:raise AgentProblem("当前店铺没有该订单，或订单尚未导入")
    report=calculate(db,store_id,start,end,**selection)
    safe,aliases=safe_data(report)
    base=os.environ.get("MULTISTORE_MODEL_URL","").strip();key=os.environ.get("MULTISTORE_MODEL_API_KEY","").strip();model=os.environ.get("MULTISTORE_MODEL_NAME","").strip()
    if not (base and key and model): raise AgentProblem("尚未配置模型；可以先使用利润看板和明细")
    specs=[{"type":"function","function":{"name":name,"description":desc,"parameters":{"type":"object","properties":{},"additionalProperties":False}}} for name,desc in TOOLS.items()]
    messages=[{"role":"system","content":"你是店铺经营助手。只调用回答问题必需的工具，只回答所问内容，最多三句，不要输出表格、编号、排名、统计比例或额外数字。所有数字必须原样来自工具结果，不得自行计算。空值表示未知，不能写成零。待核对退款快照必须注明尚待核对。不要输出最近导入时间，客户端会单独展示。不可推测缺失值或声称净利润。匿名商品名称原样保留。"},
              {"role":"user","content":f"请{intent}；区间 {start} 至 {end}。"}]
    trace=[];evidence=[];usage_total={};answer=None
    try:
        for _ in range(4):
            msg,usage=model_call(messages,specs,base,key,model)
            for k in ("prompt_tokens","completion_tokens"):
                if isinstance(usage.get(k),int): usage_total[k]=usage_total.get(k,0)+usage[k]
            calls=msg.get("tool_calls") or []
            if not calls:
                if not trace: raise AgentProblem("模型没有查询数据，未返回答案")
                answer=msg.get("content")
                if not isinstance(answer,str) or not answer.strip() or len(answer)>5000: raise AgentProblem("模型回答格式无效")
                break
            if len(calls)>3 or len(trace)+len(calls)>6: raise AgentProblem("模型调用次数超出限制")
            messages.append(msg)
            for call in calls:
                fn=call.get("function",{});name=fn.get("name");cid=call.get("id")
                if name not in TOOLS or not isinstance(cid,str): raise AgentProblem("模型请求了未授权的工具")
                try: args=json.loads(fn.get("arguments") or "{}")
                except (ValueError,TypeError): raise AgentProblem("模型工具参数无效") from None
                if args!={}: raise AgentProblem("模型不能指定店铺或修改查询范围")
                result=safe[name];trace.append({"tool":name,"status":"ok"});evidence.append(result)
                messages.append({"role":"tool","tool_call_id":cid,"content":json.dumps(result,ensure_ascii=False)})
        if answer is None: raise AgentProblem("模型未能完成查询")
        chosen={step["tool"] for step in trace}
        if any(x in question for x in ("退款","退货","退单")) and "get_refund_summary" not in chosen:
            raise AgentProblem("模型未查询退款汇总，请重试")
        if any(x in question for x in ("利润","赚","收入","成本","费用","销售","亏","经营")) and "get_profit_summary" not in chosen:
            raise AgentProblem("模型未查询利润汇总，请重试")
        if any(x in question for x in ("实付","支付金额","买家支付")) and "get_order_details" not in chosen:
            raise AgentProblem("模型未查询订单明细，请重试")
        allowed=normal_numbers(json.dumps(evidence,ensure_ascii=False))
        model_numbers=normal_numbers(answer)
        # A number appearing in evidence does not prove the model attached it to the right metric.
        # Keep its untrusted prose out of the result; render only server-calculated facts.
        verified=verified_answer(report,question,{v:k for k,v in aliases.items()})
        meta={"tools":trace,"usage":usage_total,"output_mode":"verified_summary","model_numeric_mismatch":not model_numbers.issubset(allowed)}
        db.add(AgentRun(store_id=store_id,model=model,status="ok",trace=meta));db.commit()
        return {"answer":verified,"trace":trace,"evidence":evidence,"aliases":aliases,"start":start,"end":end,"as_of":report["as_of"],"batch_ids":report["batch_ids"],"output_mode":"verified_summary","cashflow_review_version":report["cashflow_review_version"]}
    except AgentProblem:
        db.add(AgentRun(store_id=store_id,model=model,status="rejected",trace={"tools":trace,"usage":usage_total}));db.commit();raise
