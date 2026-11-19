from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from .card_query_ai import find_ego_browser
from .importer import ImportProblem, serialize_sqlite_writes
from .models import ChatKnowledgeItem, ChatSession, OrderItem, Store, now_iso

REDACTED_TOKEN = "[已自动脱敏凭据]"
_BROWSER_SENTINEL = "__MS_QN_CHAT_SYNC__"

BROWSER_GUIDE_MESSAGE = (
    "为保护你正在操作的浏览器窗口，未强行接管千牛标签页。"
    "你可以直接在千牛接待窗按 Ctrl+A、Ctrl+C 复制对话，再点『一键读取剪贴板』自动整理。"
)

PRODUCT_CATEGORY_LABELS: dict[str, str] = {
    "gemini_finished": "Gemini 成品号",
    "gemini_activation": "Gemini 激活链接/代充",
    "gemini_family": "Gemini 家庭组",
    "muse_finished": "Muse 成品号/邀请码",
    "gmail_blank": "Gmail 白号",
    "other": "其他商品",
}

KNOWLEDGE_CATEGORY_LABELS: dict[str, str] = {
    "all": "全部通用",
    **PRODUCT_CATEGORY_LABELS,
}

STAGE_LABELS: dict[str, str] = {
    "presale": "售前咨询",
    "delivery": "售中交付",
    "aftersale": "售后排障",
    "refund": "退款纠纷",
}

TOPIC_LABELS: dict[str, str] = {
    "login_2fa": "登录与2FA验证",
    "activation_region": "激活链接与地区风控",
    "family_invite": "家庭组加入问题",
    "quota_duration": "会员时长与额度疑问",
    "warranty_replace": "质保掉线与换号",
    "refund_dispute": "退款与仅退款协商",
    "bulk_pricing": "批量优惠与改价",
    "new_demand": "新品与未满足需求",
    "usage_tutorial": "使用教程与改密",
    "general": "其他常规沟通",
}

SENTIMENT_LABELS: dict[str, str] = {
    "urgent": "急迫/投诉",
    "confused": "卡壳求助",
    "neutral": "平常",
    "resolved": "已顺利解决",
}

_TOPIC_PRIORITY: tuple[str, ...] = (
    "refund_dispute",
    "warranty_replace",
    "login_2fa",
    "activation_region",
    "family_invite",
    "new_demand",
    "quota_duration",
    "bulk_pricing",
    "usage_tutorial",
    "general",
)

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "login_2fa": (
        "2fa",
        "两步验证",
        "二步验证",
        "验证码",
        "接码",
        "验证器",
        "authenticator",
        "登不上",
        "登录不上",
        "无法登录",
        "登录失败",
        "密码错误",
        "账号异常",
        "首登",
        "设备验证",
        "辅助邮箱验证",
        "安全验证",
        "谷歌验证",
    ),
    "activation_region": (
        "地区不支持",
        "国家不支持",
        "不在支持",
        "此优惠不可用",
        "不符合条件",
        "无法兑换",
        "激活失败",
        "无法激活",
        "激活不了",
        "链接打不开",
        "打不开链接",
        "链接失效",
        "风控",
        "锁区",
        "美区ip",
        "美国节点",
        "切换节点",
        "梯子",
        "绑卡失败",
        "付款资料",
    ),
    "family_invite": (
        "家庭组",
        "加入家庭",
        "家庭邀请",
        "不同国家",
        "同区",
        "已在家庭",
        "12个月内",
        "切换家庭",
        "踢出家庭",
        "车位",
        "上车",
    ),
    "quota_duration": (
        "几个月",
        "18个月",
        "15个月",
        "12个月",
        "有效期",
        "到期时间",
        "会员时长",
        "混发",
        "不足18",
        "只有14个月",
        "只有15个月",
        "额度",
        "次数限制",
        "满血",
        "2tb",
        "词元",
        "缩水",
    ),
    "warranty_replace": (
        "掉线",
        "掉会员",
        "掉pro",
        "会员没了",
        "变成免费",
        "变普通号",
        "封号",
        "被封",
        "账号停用",
        "已停用",
        "失效了",
        "换号",
        "补发",
        "换新",
        "质保换",
        "售后换",
    ),
    "refund_dispute": (
        "退款",
        "退钱",
        "仅退款",
        "退单",
        "申请售后退款",
        "不想要了",
        "拍错了",
        "退一下",
        "小二介入",
        "平台介入",
        "投诉商家",
        "给差评",
    ),
    "bulk_pricing": (
        "便宜点",
        "有优惠吗",
        "能优惠",
        "打折",
        "改价",
        "批量",
        "团购",
        "批发",
        "多买",
        "拿货",
        "代理价",
        "长期合作",
        "十个",
        "10个",
        "5个以上",
    ),
    "new_demand": (
        "chatgpt",
        "gpt plus",
        "gpt4",
        "gpt-4",
        "gpt会员",
        "claude",
        "克劳德",
        "cursor",
        "copilot",
        "midjourney",
        "perplexity",
        "grok",
        "api key",
        "apikey",
        "苹果id",
        "apple id",
        "youtube会员",
        "netflix",
    ),
    "usage_tutorial": (
        "怎么改密",
        "改密码",
        "修改密码",
        "怎么改辅助邮箱",
        "使用教程",
        "怎么使用",
        "怎么用",
        "在哪里登录",
        "登录网址",
        "官网地址",
        "app怎么下",
        "客户端下载",
        "怎么切换模型",
        "怎么设置中文",
    ),
}

_DEMAND_SIGNAL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("询问ChatGPT Plus", ("chatgpt", "gpt plus", "gpt4", "gpt-4", "gpt会员", "openai账号")),
    ("询问Claude账号", ("claude", "克劳德", "anthropic")),
    ("询问Cursor/编程工具", ("cursor", "copilot", "windsurf")),
    ("询问Grok/Perplexity等新品", ("grok", "perplexity", "midjourney")),
    ("询问API密钥/转发额度", ("api key", "apikey", "api额度", "接口key", "中转key")),
    ("批量团购需求", ("批量", "团购", "批发", "拿货", "代理", "多买", "10个", "十个", "5个以上", "长期拿", "工作室采购")),
    ("要求包月/更长质保", ("包月", "月卡", "全期质保", "质保多久", "掉线包换", "包售后", "质保太短", "包年质保")),
    ("需要详细2FA接码教程", ("2fa", "两步验证", "二步验证", "接码", "验证器", "验证码怎么获取", "验证码怎么收")),
    ("指定美区/日区账号", ("美区", "美国区", "原生美", "日区", "日本区", "港区", "新加坡区", "台湾区", "指定地区")),
    ("希望免绑卡/代激活服务", ("免绑卡", "无需绑卡", "没有外币卡", "帮我激活", "代激活", "代充", "包激活")),
    ("关注独享与隐私安全", ("是独享吗", "独享还是共享", "别人能看到", "聊天记录安全", "能改密码吗", "完全独享")),
    ("关注足月时长与混发说明", ("足月", "满18个月", "混发", "为什么只有15个月", "不足18个月", "实际时长")),
)

DEFAULT_KNOWLEDGE_TEMPLATES: dict[str, dict[str, str]] = {
    "login_2fa": {
        "title": "【登录与2FA验证】首登接码与两步验证标准指引",
        "trigger_question": "Gemini成品号首次登录提示需要2FA两步验证码怎么操作？",
        "standard_reply": (
            "亲，您好～账号开启了谷歌标准两步验证（2FA）来保护防封哦！请按以下3步轻松登录：\n"
            "1. 打开谷歌/Gemini官网输入发货给您的账号与初始密码；\n"
            "2. 页面提示输入6位动态验证码时，打开2FA在线接码工具（或身份验证器App），粘贴发货信息最后一段2FA密钥获取实时6位数字；\n"
            "3. 若弹出确认辅助邮箱，请选择『确认您的辅助邮箱地址』并完整输入发货附带的辅助邮箱（注意不是收验证码，直接填完整邮箱地址即可）。如仍卡住请截图发我，马上帮您排查！"
        ),
    },
    "activation_region": {
        "title": "【激活链接与地区风控】优惠不可用/地区不支持排障话术",
        "trigger_question": "打开Gemini激活链接提示此优惠不可用或所在地区不支持怎么办？",
        "standard_reply": (
            "亲，出现『此优惠不可用 / 地区不支持』通常是当前浏览器环境或账号资料地区不匹配导致的，请按以下步骤处理：\n"
            "1. 请开启纯净美区（美国）全局网络节点，建议用 Chrome 无痕窗口（Ctrl+Shift+N）重新打开激活链接；\n"
            "2. 检查您谷歌账号的 Google Pay 付款资料是否绑定过其他国家地区，若有旧资料可在 pay.google.com 关闭非美区付款资料后重试；\n"
            "3. 若您的账号此前已领过同类型学生/试用权益导致无法再次兑换，请随时发截图联系我为您协调换链或代激活方案哈～"
        ),
    },
    "family_invite": {
        "title": "【家庭组加入问题】提示不同国家/无法加入家庭组解决话术",
        "trigger_question": "接受Gemini家庭组邀请提示与家庭管理员不在同一国家怎么办？",
        "standard_reply": (
            "亲，加入谷歌家庭组要求您的谷歌账号归属区与车头一致（美区）且近12个月未频繁切换过家庭组哦：\n"
            "1. 请先连接美国节点，打开浏览器无痕窗口登录您的谷歌账号后再点邀请链接；\n"
            "2. 若提示『您与家庭群组管理员不在同一个国家/地区』，请前往 pay.google.com → 设置，检查付款资料国家是否为美国（US），删除旧地区资料后新建美区资料即可顺利上车；\n"
            "3. 如提示『12个月内只能切换一次家庭群组』，说明该号近期进出过其他家庭组，建议更换一个未进过家庭组的谷歌号加入哈。"
        ),
    },
    "quota_duration": {
        "title": "【会员时长与额度疑问】混发时长区间与Pro满血权益说明",
        "trigger_question": "为什么显示到期时间是15个月左右？Gemini Pro额度是满血独享吗？",
        "standard_reply": (
            "亲，放心哈！正如宝贝详情页说明，本批次教育/官方优惠批次由于谷歌激活账期结算规则，实际到账时长在标注区间内属于正常足额批次，期间享受完整 Google One AI Premium（满血 Gemini Advanced / Pro 模型 + 2TB 云空间）全部独享权益，聊天记录与云盘完全独立私密，绝非共享号。质保期内有任何异常随时找我售后保障！"
        ),
    },
    "warranty_replace": {
        "title": "【质保掉线与换号】账号风控停用或会员掉线售后换新话术",
        "trigger_question": "刚用的账号提示被停用或者Pro会员掉了怎么售后换号？",
        "standard_reply": (
            "亲，非常抱歉让您久等了！请先别着急申请仅退款哈，小店承诺质保期内非人为违规导致的风控停用或会员掉线均包售后处理：\n"
            "1. 请把当前登录报错页面或会员状态截图发我核实一下，并发一下您的订单编号；\n"
            "2. 我这边马上为您核查批次，确认无法恢复后立即为您免费补发全新独享账号！\n"
            "温馨提示：新号首登建议固定常用纯净节点，前48小时内暂缓频繁跨区切换IP或高频改动安全资料，稳定性会好很多哦～"
        ),
    },
    "refund_dispute": {
        "title": "【退款与仅退款协商】虚拟商品温和挽留与拍错退换话术",
        "trigger_question": "不会操作不想要了/拍错了可以退款吗？",
        "standard_reply": (
            "亲，先别着急哈～如果是卡在登录验证或激活步骤，其实只需1分钟就能搞定，您可以把卡住的画面截图发我，我一步步手把手教您登上去！\n"
            "如果您确实是拍错规格（例如想买成品号拍成了激活链接）且卡密/链接尚未使用，这边核实未核销后可以为您无忧退换对应规格；麻烦稍等1-2分钟让我帮您核验卡密状态哈，一定给您妥善解决！"
        ),
    },
    "bulk_pricing": {
        "title": "【批量优惠与改价】工作室多买/长期拿货阶梯优惠话术",
        "trigger_question": "一次买5个/10个以上或者长期拿货有批量团购优惠吗？",
        "standard_reply": (
            "亲，欢迎工作室与老客户批量采购！单次拍5件及以上或长期稳定拿货均可享受阶梯团购价，并且同样包含完整售后质保与优先补发通道。\n"
            "您可以先告诉我需要的具体品类（如 Gemini 成品号 / 激活链接 / Gmail 白号）和大概数量，提交待付款订单后发我一下，我马上在后台为您核算优惠改价哈～"
        ),
    },
    "new_demand": {
        "title": "【新品与未满足需求】询问其他AI会员选品登记与替代推荐话术",
        "trigger_question": "请问店里有ChatGPT Plus或者Claude独享账号吗？",
        "standard_reply": (
            "亲，感谢您的关注！目前小店现货主营高性价比的 Google Gemini Pro/Ultra 独享成品号、本人号激活链接、Gemini 家庭组及 Muse 词元号（长文本分析与代码能力也非常强，性价比极高）。\n"
            "您咨询的新品需求我已帮您重点登记反馈给选品负责人啦，后续上新稳定批次会第一时间上架，也欢迎先体验咱家口碑款 Gemini Pro 独享会员哦～"
        ),
    },
    "usage_tutorial": {
        "title": "【使用教程与改密】独享成品号安全改密与日常防风控指南",
        "trigger_question": "收到Gemini成品号后怎么修改密码和辅助邮箱？有什么注意事项？",
        "standard_reply": (
            "亲，账号为您一人完全独享，支持自行修改密码与安全资料！为了让账号更稳固不触发谷歌异地风控，建议按以下节奏操作：\n"
            "1. 首登成功后建议先固定同一节点正常使用 3～5 天，让谷歌信任您的常用设备与IP环境；\n"
            "2. 稳定后再前往 myaccount.google.com →『安全性』依次修改登录密码、换绑您自己的辅助邮箱和2FA验证器；\n"
            "3. 请妥善保管好您修改后的新密码与2FA密钥，日常尽量避免短时间内多国IP来回跳动即可长期稳定使用哈！"
        ),
    },
}

_EMAIL_CHAIN_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"(?:(?:\s*(?:----+|---+|\|\|+|\||/|----|,|，|;)\s*|\s+)[A-Za-z0-9!@#$%^&*()_+\-=\[\]{}.:?~]{4,})+"
)
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)(https?://[^\s\"'<>]+?[?&](?:token|code|key|secret|invite|ticket|redeem|auth|pwd|password|sig|signature)=)([^&\s\"'<>]+)"
)
_URL_PATH_SECRET_RE = re.compile(
    r"(?i)(https?://(?:one\.google\.com/offer|g\.co/[^\s/]+|[^\s\"'<>]*?(?:redeem|activate|claim|invite|offer))/)([A-Za-z0-9._~%-]{6,})"
)
_LABELED_MULTILINE_SECRET_RE = re.compile(
    r"(?i)(账号密码|登录密码|初始密码|新密码|密码|卡密|兑换码|激活码|邀请码|密钥|2FA密钥|谷歌密钥|辅助邮箱|辅邮|口令|password|passwd|pwd|secret|token|api[_\s-]?key)"
    r"\s*[:=：]\s*\n\s*([A-Za-z0-9!@#$%^&*()_+\-=\[\]{}.:?~]{4,80})"
)
_LABELED_INLINE_SECRET_RE = re.compile(
    r"(?i)(账号密码|登录密码|初始密码|新密码|密码|卡密|兑换码|激活码|邀请码|密钥|2FA密钥|谷歌密钥|辅助邮箱|辅邮|口令|password|passwd|pwd|secret|token|api[_\s-]?key)"
    r"(?:\s*[:=：]\s*|\s*(?:是|为)\s*|\s+)"
    r"(?!(?:2fa|totp|otp)(?![A-Za-z0-9]))"
    r"((?:[A-Za-z2-7]{4}[ -]){3,7}[A-Za-z2-7]{4}|[A-Za-z0-9!@#$%^&*()_+\-=\[\]{}.:?~@-]{5,120})"
)
_SPACED_BASE32_RE = re.compile(r"\b(?:[A-Za-z2-7]{4}[ -]){3,7}[A-Za-z2-7]{4}\b")
_UPPER_BASE32_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Z2-7]{16,64}(?![A-Za-z0-9]))(?=[A-Z2-7]*[A-Z][A-Z2-7]*[A-Z])[A-Z2-7]{16,64}(?![A-Za-z0-9])"
)
_LOWER_LONG_BASE32_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[a-z2-7]{24,64}(?![A-Za-z0-9]))(?=[a-z2-7]*[a-z][a-z2-7]*[a-z])(?=[a-z2-7]*[2-7])[a-z2-7]{24,64}(?![A-Za-z0-9])"
)
_MIXED_SECRET_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?!(?:FF|LD)\d{6}[A-Za-z0-9]{4,10}(?![A-Za-z0-9]))(?=[A-Za-z0-9]{18,64}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{18,64}(?![A-Za-z0-9])"
)
_CDK_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?=[A-Za-z0-9-]{14,40}(?![A-Za-z0-9-]))(?=[A-Za-z0-9-]*[A-Za-z])(?:[A-Za-z0-9]{4,6}-){2,5}[A-Za-z0-9]{4,6}(?![A-Za-z0-9-])"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ORDER_ID_RE = re.compile(r"(?<!\d)(\d{15,22})(?!\d)")
_ORDER_ANCHOR_RE = re.compile(r"(?:订单编号|订单号|主订单号|子订单号|单号)\s*[:：#]?\s*(\d{15,22})(?!\d)")

_DATE_BANNER_RE = re.compile(
    r"^\s*[-=~*—]{0,8}\s*(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?)\s*[-=~*—]{0,8}\s*$"
)
_DIVIDER_LINE_RE = re.compile(r"^\s*(?:={3,}|-{3,}|_{3,}|\*{3,}|#{2,}\s*(?:会话|对话|客户|买家).*)\s*$")

_TS_FIRST_HEADER_RE = re.compile(
    r"^\[?(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(.+)$"
)
_SENDER_TS_HEADER_RE = re.compile(
    r"^(.{1,50}?)\s+[\(\[（【]?("
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{1,2}:\d{2}:\d{2}"
    r")[\)\]）】]?\s*[:：]?\s*(.*)$"
)
_BRACKET_ROLE_RE = re.compile(
    r"^【\s*(买家|客户|用户|访客|会员|客服|商家|卖家|掌柜|售后|售前|系统[^】]*)\s*】\s*[:：]?\s*(.*)$"
)
_EXPLICIT_ROLE_PREFIX_RE = re.compile(
    r"^(买家|客户|用户|访客|会员|问|Q|q|客服|商家|卖家|掌柜|店主|店小二|售后|售前|答|A|a|系统消息|系统提示|系统)\s*[:：]\s*(.*)$"
)
_SUBACCOUNT_PREFIX_RE = re.compile(
    r"^([^\s:：]{1,24}:[^\s:：]{1,24})\s*[:：]\s*(.+)$"
)
_NICK_COLON_LINE_RE = re.compile(
    r"^([^\s:：\[\]【】]{1,20})\s*[:：]\s*(.+)$"
)

_METADATA_TIME_LABELS = {
    "付款时间",
    "下单时间",
    "创建时间",
    "成交时间",
    "发货时间",
    "完结时间",
    "退款时间",
    "支付时间",
    "订单时间",
    "会话时间",
    "质检时间",
    "时间",
}

_NON_SPEAKER_COLON_LABELS = {
    "订单编号",
    "订单号",
    "主订单号",
    "子订单号",
    "实付",
    "实付金额",
    "宝贝",
    "商品",
    "商品名称",
    "宝贝标题",
    "颜色分类",
    "规格",
    "状态",
    "订单状态",
    "退款状态",
    "退款原因",
    "退款金额",
    "账号",
    "账号密码",
    "登录密码",
    "初始密码",
    "密码",
    "卡密",
    "兑换码",
    "激活码",
    "邀请码",
    "密钥",
    "2fa",
    "2fa密钥",
    "谷歌密钥",
    "辅助邮箱",
    "辅邮",
    "注意",
    "温馨提示",
    "提示",
    "教程",
    "链接",
    "网址",
    "地址",
    "http",
    "https",
    *_METADATA_TIME_LABELS,
}

_GENERIC_BUYER_NAMES = {
    "",
    "买家",
    "客户",
    "用户",
    "访客",
    "会员",
    "匿名买家",
    "问",
    "q",
    "buyer",
    "user",
    "customer",
}

_GENERIC_SELLER_NAMES = {
    "客服",
    "商家",
    "卖家",
    "掌柜",
    "店主",
    "店小二",
    "小二",
    "售后",
    "售前",
    "答",
    "a",
    "我",
    "seller",
    "agent",
}

_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "buyer_nick": (
        "会员名称",
        "买家昵称",
        "买家旺旺",
        "旺旺昵称",
        "客户昵称",
        "客户名称",
        "买家",
        "客户",
        "buyer_nick",
        "buyer",
        "nick",
    ),
    "order_id": (
        "订单编号",
        "订单号",
        "主订单编号",
        "子订单编号",
        "关联订单",
        "关联订单号",
        "order_external_id",
        "order_id",
        "tid",
    ),
    "seller_nick": (
        "接待客服",
        "客服昵称",
        "客服子账号",
        "子账号",
        "客服账号",
        "客服",
        "seller_nick",
        "seller",
        "agent",
    ),
    "check_item": (
        "检出项",
        "质检项",
        "命中规则",
        "质检标签",
        "问题类型",
        "风险项",
        "违规项",
        "标签",
        "check_item",
        "topic",
        "tag",
    ),
    "chat_text": (
        "命中会话文本",
        "命中会话",
        "会话文本",
        "会话内容",
        "聊天记录",
        "聊天内容",
        "对话内容",
        "消息内容",
        "咨询内容",
        "文本内容",
        "详情",
        "chat_text",
        "messages",
        "content",
        "dialogue",
        "text",
    ),
    "started_at": (
        "会话时间",
        "聊天时间",
        "接待时间",
        "质检时间",
        "发送时间",
        "消息时间",
        "创建时间",
        "时间",
        "started_at",
        "timestamp",
        "time",
        "created_at",
    ),
    "product": (
        "商品名称",
        "商品标题",
        "宝贝标题",
        "咨询商品",
        "商品信息",
        "商品",
        "SKU",
        "sku",
        "product",
        "title",
    ),
    "sender": (
        "发送人",
        "发送方",
        "发言人",
        "角色",
        "sender",
        "role",
        "from",
    ),
    "session_key": (
        "会话ID",
        "会话id",
        "会话编号",
        "session_id",
    ),
}


def sanitize_chat_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return ""
    text = _EMAIL_CHAIN_RE.sub(REDACTED_TOKEN, text)
    text = _URL_QUERY_SECRET_RE.sub(rf"\1{REDACTED_TOKEN}", text)
    text = _URL_PATH_SECRET_RE.sub(rf"\1{REDACTED_TOKEN}", text)
    text = _LABELED_MULTILINE_SECRET_RE.sub(rf"\1：{REDACTED_TOKEN}", text)
    text = _LABELED_INLINE_SECRET_RE.sub(rf"\1：{REDACTED_TOKEN}", text)
    text = _SPACED_BASE32_RE.sub(REDACTED_TOKEN, text)
    text = _UPPER_BASE32_RE.sub(REDACTED_TOKEN, text)
    text = _LOWER_LONG_BASE32_RE.sub(REDACTED_TOKEN, text)
    text = _MIXED_SECRET_TOKEN_RE.sub(REDACTED_TOKEN, text)
    text = _CDK_KEY_RE.sub(REDACTED_TOKEN, text)
    text = _EMAIL_RE.sub(REDACTED_TOKEN, text)
    text = _PHONE_RE.sub(REDACTED_TOKEN, text)
    text = re.sub(rf"(?:{re.escape(REDACTED_TOKEN)}[\s\-|/,:;，：；]*){{2,}}", REDACTED_TOKEN, text)
    return text.strip()


def mask_buyer_nick(raw_nick: Any) -> str:
    cleaned = str(raw_nick or "").strip()
    cleaned = re.sub(r"^(?:买家昵称|会员名称|买家旺旺|客户昵称|买家|客户)\s*[:：]\s*", "", cleaned).strip()
    if not cleaned or cleaned.lower() in _GENERIC_BUYER_NAMES:
        return "匿名买家"
    if "*" in cleaned:
        return cleaned[:120]
    cleaned = sanitize_chat_text(cleaned)
    if cleaned == REDACTED_TOKEN:
        return "匿名买家"
    if len(cleaned) == 1:
        return f"{cleaned}**"
    return f"{cleaned[0]}**{cleaned[-1]}"[:120]


def mask_seller_nick(raw_nick: Any) -> str:
    cleaned = str(raw_nick or "").strip()
    if not cleaned:
        return "客服"
    if ":" in cleaned:
        parts = cleaned.split(":", 1)
        suffix = parts[1].strip() or "客服"
        return f"客服:{suffix}"[:60]
    if "：" in cleaned:
        parts = cleaned.split("：", 1)
        suffix = parts[1].strip() or "客服"
        return f"客服:{suffix}"[:60]
    if cleaned.lower() in {"答", "a", "seller", "agent", "我"}:
        return "客服"
    return sanitize_chat_text(cleaned)[:60] or "客服"


def _normalize_timestamp_str(raw_ts: str, current_date: str = "") -> str:
    val = str(raw_ts or "").strip()
    if not val:
        return ""
    val = val.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    val = re.sub(r"\s+", " ", val).strip()
    full_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", val)
    if full_match:
        yr, mo, dy, hr, mn, sc = full_match.groups()
        return f"{int(yr):04d}-{int(mo):02d}-{int(dy):02d} {int(hr):02d}:{int(mn):02d}:{int(sc or 0):02d}"
    short_date_match = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", val)
    if short_date_match:
        mo, dy, hr, mn, sc = short_date_match.groups()
        yr = int(current_date[:4]) if len(current_date) >= 4 and current_date[:4].isdigit() else datetime.now().year
        return f"{yr:04d}-{int(mo):02d}-{int(dy):02d} {int(hr):02d}:{int(mn):02d}:{int(sc or 0):02d}"
    time_only_match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", val)
    if time_only_match:
        hr, mn, sc = time_only_match.groups()
        time_part = f"{int(hr):02d}:{int(mn):02d}:{int(sc or 0):02d}"
        return f"{current_date} {time_part}" if current_date else time_part
    try:
        return datetime.fromisoformat(val).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return val[:30]


def _detect_sender_role(sender: str, store_name: str = "") -> str:
    cleaned = sender.strip().strip("[]【】()（）").strip()
    lowered = cleaned.lower()
    if not cleaned:
        return "buyer"
    if any(sys_word in cleaned for sys_word in ("系统消息", "系统提示", "千牛系统", "温馨提示", "官方提醒")) or lowered == "system":
        return "system"
    if lowered in _GENERIC_BUYER_NAMES:
        return "buyer"
    if lowered in _GENERIC_SELLER_NAMES:
        return "seller"
    if ":" in cleaned or "：" in cleaned:
        return "seller"
    seller_markers = ("客服", "掌柜", "店小二", "小二", "售前", "售后", "旗舰店", "专营店", "专卖店", "机器人", "店长", "值班")
    if any(marker in cleaned for marker in seller_markers):
        return "seller"
    norm_store = (store_name or "").strip().lower()
    if len(norm_store) >= 2 and norm_store not in {"shop", "store", "test"} and norm_store in lowered:
        return "seller"
    return "buyer"


def classify_product_category(text: str) -> str:
    haystack = (text or "").lower()
    if not haystack.strip():
        return "other"
    if any(kw in haystack for kw in ("muse", "缪斯", "10亿词元", "词元")):
        return "muse_finished"
    if any(kw in haystack for kw in ("家庭组", "家庭共享", "加入家庭", "家庭邀请", "车位", "上车", "拼车")):
        return "gemini_family"
    if any(
        kw in haystack
        for kw in (
            "激活链接",
            "兑换链接",
            "激活资格",
            "代充",
            "代开通",
            "代激活",
            "直充",
            "升级链接",
            "开通链接",
            "无需绑卡",
            "one.google.com/offer",
        )
    ):
        return "gemini_activation"
    if any(kw in haystack for kw in ("白号", "gmail白号", "谷歌白号", "纯净号", "全新gmail", "空号")):
        return "gmail_blank"
    if any(
        kw in haystack
        for kw in (
            "gemini",
            "成品号",
            "独享已激活",
            "直接使用",
            "ultra",
            "ai premium",
            "满血pro",
        )
    ):
        return "gemini_finished"
    return "other"


def _extract_order_external_id(raw_text: str) -> str | None:
    anchored = _ORDER_ANCHOR_RE.findall(raw_text or "")
    if anchored:
        return anchored[0]
    candidates = _ORDER_ID_RE.findall(raw_text or "")
    return candidates[0] if candidates else None


def _decode_input_bytes(text_or_bytes: str | bytes) -> tuple[str, bytes | None]:
    if isinstance(text_or_bytes, str):
        return text_or_bytes, None
    raw_bytes = bytes(text_or_bytes)
    if raw_bytes.startswith(b"PK\x03\x04"):
        return "", raw_bytes
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw_bytes.decode(encoding), raw_bytes
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace"), raw_bytes


def _match_table_headers(headers: list[str]) -> dict[str, str]:
    normalized_map = {hdr: re.sub(r"\s+", "", hdr).lower() for hdr in headers if hdr}
    matched: dict[str, str] = {}
    used_headers: set[str] = set()
    for field_key, aliases in _TABLE_ALIASES.items():
        for alias in aliases:
            alias_norm = alias.lower()
            found = next(
                (hdr for hdr, norm in normalized_map.items() if hdr not in used_headers and norm == alias_norm),
                "",
            )
            if not found and len(alias_norm) >= 2:
                found = next(
                    (hdr for hdr, norm in normalized_map.items() if hdr not in used_headers and alias_norm in norm),
                    "",
                )
            if found:
                matched[field_key] = found
                used_headers.add(found)
                break
    return matched


def _read_xlsx_rows(raw_bytes: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 80 * 1024 * 1024:
                raise ImportProblem("XLSX 表格解压后体积过大")
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        sheet = workbook.active
        if sheet is None:
            workbook.close()
            raise ImportProblem("XLSX 文件没有工作表")
        matrix: list[list[Any]] = []
        for sheet_row in sheet.iter_rows(values_only=True):
            if any(cell is not None and str(cell).strip() for cell in sheet_row):
                matrix.append(list(sheet_row))
            if len(matrix) > 20001:
                workbook.close()
                raise ImportProblem("单次最多导入 20000 行记录")
        workbook.close()
    except ImportProblem:
        raise
    except Exception as exc:
        raise ImportProblem("XLSX 聊天/质检文件无法解析") from exc
    if len(matrix) < 2:
        raise ImportProblem("XLSX 文件没有有效数据行")
    headers = [
        (str(col).strip() if col is not None and str(col).strip() else f"col_{idx}")
        for idx, col in enumerate(matrix[0])
    ]
    dedup_headers: list[str] = []
    seen: Counter[str] = Counter()
    for hdr in headers:
        seen[hdr] += 1
        dedup_headers.append(hdr if seen[hdr] == 1 else f"{hdr}_{seen[hdr]}")
    rows = [dict(zip(dedup_headers, vals)) for vals in matrix[1:]]
    return dedup_headers, rows


def _try_parse_markdown_table(text: str) -> tuple[list[str], list[dict[str, Any]]] | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    sep_idx = -1
    for idx in range(1, min(4, len(lines))):
        candidate = lines[idx]
        if "|" in candidate and re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$", candidate):
            sep_idx = idx
            break
    if sep_idx < 1:
        return None
    header_line = lines[sep_idx - 1].strip("|")
    headers = [cell.strip() for cell in header_line.split("|")]
    if len([hdr for hdr in headers if hdr]) < 2:
        return None
    rows: list[dict[str, Any]] = []
    for raw_ln in lines[sep_idx + 1 :]:
        if "|" not in raw_ln:
            break
        cells = [cell.strip() for cell in raw_ln.strip("|").split("|")]
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        row_dict = dict(zip(headers, cells[: len(headers)]))
        if any(val for val in row_dict.values()):
            rows.append(row_dict)
    return (headers, rows) if rows else None


def _try_parse_delimited_table(text: str, filename: str) -> tuple[list[str], list[dict[str, Any]]] | None:
    lower_name = (filename or "").lower()
    non_empty = [ln for ln in text.splitlines() if ln.strip()]
    if len(non_empty) < 2:
        return None
    first_line = non_empty[0]
    is_explicit_file = lower_name.endswith((".csv", ".tsv"))
    delimiter = ""
    if lower_name.endswith(".tsv") or "\t" in first_line:
        delimiter = "\t"
    elif lower_name.endswith(".csv") or ("," in first_line and first_line.count(",") >= 1):
        delimiter = ","
    if not delimiter:
        return None
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        parsed_rows = [row for row in reader if any(cell.strip() for cell in row)]
    except csv.Error:
        return None
    if len(parsed_rows) < 2 or len(parsed_rows[0]) < 2:
        return None
    raw_headers = [cell.strip() or f"col_{idx}" for idx, cell in enumerate(parsed_rows[0])]
    mapping = _match_table_headers(raw_headers)
    if not is_explicit_file and len(mapping) < 2:
        return None
    if is_explicit_file and "chat_text" not in mapping and len(mapping) < 2:
        return None
    dedup_headers: list[str] = []
    seen: Counter[str] = Counter()
    for hdr in raw_headers:
        seen[hdr] += 1
        dedup_headers.append(hdr if seen[hdr] == 1 else f"{hdr}_{seen[hdr]}")
    dict_rows: list[dict[str, Any]] = []
    for vals in parsed_rows[1:]:
        padded = vals + [""] * max(0, len(dedup_headers) - len(vals))
        dict_rows.append(dict(zip(dedup_headers, padded[: len(dedup_headers)])))
    return dedup_headers, dict_rows


def _try_parse_json_records(text: str, filename: str) -> list[dict[str, Any]] | None:
    stripped = text.strip()
    is_json_file = (filename or "").lower().endswith(".json")
    if not is_json_file and not (stripped.startswith("[") or stripped.startswith("{")):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if is_json_file:
            raise ImportProblem("JSON 聊天文件格式无效，请检查文件内容") from None
        return None
    if isinstance(payload, dict):
        for key in ("sessions", "rows", "data", "items", "records"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return None
    dict_items = [entry for entry in payload if isinstance(entry, dict)]
    if is_json_file and not dict_items:
        raise ImportProblem("JSON 聊天文件为空，没有可导入的会话记录")
    return dict_items or None


def _split_ts_first_payload(payload: str) -> tuple[str, str]:
    sub_colon = re.match(r"^([^\s:：]{1,28}:[^\s:：]{1,24})\s*[:：]\s*(.*)$", payload)
    if sub_colon:
        return sub_colon.group(1).strip(), sub_colon.group(2).strip()
    sub_only = re.match(r"^([^\s:：]{1,28}:[^\s:：]{1,24})$", payload.strip())
    if sub_only:
        return sub_only.group(1).strip(), ""
    cn_colon = payload.find("：")
    ascii_colon_sp = payload.find(": ")
    split_pos = -1
    sep_len = 1
    if cn_colon > 0 and (ascii_colon_sp < 0 or cn_colon < ascii_colon_sp):
        split_pos = cn_colon
    elif ascii_colon_sp > 0:
        split_pos = ascii_colon_sp
        sep_len = 2
    elif 0 < payload.find(":") <= 20:
        split_pos = payload.find(":")
    if 0 < split_pos <= 36:
        return payload[:split_pos].strip(), payload[split_pos + sep_len :].strip()
    return payload.strip()[:36], ""


def _parse_dialogue_lines(
    raw_text: str,
    *,
    store_name: str = "",
    default_buyer: str = "",
    default_seller: str = "",
    default_time: str = "",
    allow_multi_split: bool = True,
) -> list[dict[str, Any]]:
    normalized = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    sections: list[list[str]] = [[]]
    for line in normalized.splitlines():
        if allow_multi_split and _DIVIDER_LINE_RE.match(line) and not _DATE_BANNER_RE.match(line):
            if sections[-1]:
                sections.append([])
            continue
        sections[-1].append(line)

    parsed_groups: list[dict[str, Any]] = []
    for sec_lines in sections:
        if not any(ln.strip() for ln in sec_lines):
            continue
        raw_msgs: list[dict[str, str]] = []
        current_date = ""
        if default_time and len(default_time) >= 10 and re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", default_time):
            current_date = default_time[:10].replace("/", "-")

        for raw_line in sec_lines:
            stripped = raw_line.strip()
            if not stripped:
                continue

            banner_match = _DATE_BANNER_RE.match(stripped)
            if banner_match:
                norm_dt = _normalize_timestamp_str(banner_match.group(1) + " 00:00:00")
                if len(norm_dt) >= 10:
                    current_date = norm_dt[:10]
                continue

            ts_first = _TS_FIRST_HEADER_RE.match(stripped)
            if ts_first:
                msg_time = _normalize_timestamp_str(ts_first.group(1), current_date)
                if len(msg_time) >= 10 and msg_time[4] == "-":
                    current_date = msg_time[:10]
                sender_raw, inline_text = _split_ts_first_payload(ts_first.group(2))
                if sender_raw not in _METADATA_TIME_LABELS:
                    role = _detect_sender_role(sender_raw, store_name)
                    raw_msgs.append(
                        {
                            "role": role,
                            "raw_sender": sender_raw,
                            "time": msg_time,
                            "text": inline_text,
                        }
                    )
                    continue

            sender_ts = _SENDER_TS_HEADER_RE.match(stripped)
            if sender_ts:
                cand_sender = sender_ts.group(1).strip().rstrip(":：").strip()
                if (
                    cand_sender
                    and cand_sender not in _METADATA_TIME_LABELS
                    and not cand_sender.endswith("时间")
                    and not cand_sender.startswith(("订单编号", "订单号"))
                ):
                    msg_time = _normalize_timestamp_str(sender_ts.group(2), current_date)
                    if len(msg_time) >= 10 and msg_time[4] == "-":
                        current_date = msg_time[:10]
                    role = _detect_sender_role(cand_sender, store_name)
                    raw_msgs.append(
                        {
                            "role": role,
                            "raw_sender": cand_sender,
                            "time": msg_time,
                            "text": sender_ts.group(3).strip(),
                        }
                    )
                    continue

            bracket_role = _BRACKET_ROLE_RE.match(stripped)
            if bracket_role:
                speaker = bracket_role.group(1).strip()
                role = _detect_sender_role(speaker, store_name)
                raw_msgs.append(
                    {
                        "role": role,
                        "raw_sender": default_buyer if role == "buyer" and default_buyer else (default_seller if role == "seller" and default_seller else speaker),
                        "time": default_time,
                        "text": bracket_role.group(2).strip(),
                    }
                )
                continue

            explicit_role = _EXPLICIT_ROLE_PREFIX_RE.match(stripped)
            if explicit_role:
                speaker = explicit_role.group(1).strip()
                role = _detect_sender_role(speaker, store_name)
                raw_msgs.append(
                    {
                        "role": role,
                        "raw_sender": default_buyer if role == "buyer" and default_buyer else (default_seller if role == "seller" and default_seller else speaker),
                        "time": default_time,
                        "text": explicit_role.group(2).strip(),
                    }
                )
                continue

            subacc_match = _SUBACCOUNT_PREFIX_RE.match(stripped)
            if subacc_match and not stripped.lower().startswith(("http://", "https://")):
                speaker = subacc_match.group(1).strip()
                raw_msgs.append(
                    {
                        "role": "seller",
                        "raw_sender": speaker,
                        "time": default_time,
                        "text": subacc_match.group(2).strip(),
                    }
                )
                continue

            nick_colon = _NICK_COLON_LINE_RE.match(stripped)
            if nick_colon and not stripped.lower().startswith(("http://", "https://")):
                cand_nick = nick_colon.group(1).strip()
                if cand_nick.lower() not in _NON_SPEAKER_COLON_LABELS and not cand_nick.endswith("时间"):
                    role = _detect_sender_role(cand_nick, store_name)
                    if role == "seller" or not raw_msgs or any(m["raw_sender"] == cand_nick for m in raw_msgs):
                        raw_msgs.append(
                            {
                                "role": role,
                                "raw_sender": cand_nick,
                                "time": default_time,
                                "text": nick_colon.group(2).strip(),
                            }
                        )
                        continue

            if raw_msgs:
                if raw_msgs[-1]["text"]:
                    raw_msgs[-1]["text"] += "\n" + stripped
                else:
                    raw_msgs[-1]["text"] = stripped
            else:
                inferred_role = "seller" if any(cue in stripped for cue in ("亲，", "您好，", "已为您", "质保期内")) else "buyer"
                raw_msgs.append(
                    {
                        "role": inferred_role,
                        "raw_sender": default_seller if inferred_role == "seller" and default_seller else (default_buyer or "买家"),
                        "time": default_time,
                        "text": stripped,
                    }
                )

        raw_msgs = [msg for msg in raw_msgs if msg["text"].strip()]
        if not raw_msgs:
            continue

        if allow_multi_split:
            sub_chunks: list[list[dict[str, str]]] = []
            active_buyer_key = ""
            current_chunk: list[dict[str, str]] = []
            for msg in raw_msgs:
                sender_norm = msg["raw_sender"].strip()
                is_specific_buyer = (
                    msg["role"] == "buyer"
                    and sender_norm.lower() not in _GENERIC_BUYER_NAMES
                )
                if is_specific_buyer:
                    if active_buyer_key and sender_norm != active_buyer_key and current_chunk:
                        sub_chunks.append(current_chunk)
                        current_chunk = []
                    active_buyer_key = sender_norm
                current_chunk.append(msg)
            if current_chunk:
                sub_chunks.append(current_chunk)
        else:
            sub_chunks = [raw_msgs]

        for chunk in sub_chunks:
            chunk_raw_text = "\n".join(msg["text"] for msg in chunk)
            buyer_candidates = [
                msg["raw_sender"]
                for msg in chunk
                if msg["role"] == "buyer" and msg["raw_sender"].strip().lower() not in _GENERIC_BUYER_NAMES
            ]
            chosen_raw_buyer = buyer_candidates[0] if buyer_candidates else default_buyer
            parsed_groups.append(
                {
                    "raw_buyer": chosen_raw_buyer,
                    "raw_text": chunk_raw_text,
                    "order_external_id": _extract_order_external_id(chunk_raw_text),
                    "product_hint": "",
                    "topic_hint": "",
                    "started_at_hint": default_time,
                    "raw_messages": chunk,
                }
            )
    return parsed_groups


def _records_to_session_groups(
    records: list[dict[str, Any]],
    headers: list[str],
    store_name: str = "",
) -> list[dict[str, Any]]:
    mapping = _match_table_headers(headers)

    def cell_str(row: dict[str, Any], key: str) -> str:
        col = mapping.get(key, "")
        if not col:
            return ""
        val = row.get(col)
        if val is None:
            return ""
        if isinstance(val, (datetime, date)):
            return val.strftime("%Y-%m-%d %H:%M:%S") if isinstance(val, datetime) else val.strftime("%Y-%m-%d 00:00:00")
        return str(val).strip()

    has_sender_col = "sender" in mapping and "chat_text" in mapping
    if has_sender_col and len(records) > 1:
        grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        group_order: list[str] = []
        for idx, row in enumerate(records):
            sess_key = (
                cell_str(row, "session_key")
                or cell_str(row, "order_id")
                or cell_str(row, "buyer_nick")
                or f"row_{idx}"
            )
            if sess_key not in grouped_rows:
                group_order.append(sess_key)
            grouped_rows[sess_key].append(row)

        groups: list[dict[str, Any]] = []
        for sess_key in group_order:
            rows_in_sess = grouped_rows[sess_key]
            chunk: list[dict[str, str]] = []
            buyer_raw = ""
            order_raw = ""
            product_hint = ""
            topic_hint = ""
            for row in rows_in_sess:
                msg_text = cell_str(row, "chat_text")
                if not msg_text:
                    continue
                sender_val = cell_str(row, "sender")
                buyer_col_val = cell_str(row, "buyer_nick")
                seller_col_val = cell_str(row, "seller_nick")
                if buyer_col_val and not buyer_raw:
                    buyer_raw = buyer_col_val
                if cell_str(row, "order_id") and not order_raw:
                    order_raw = cell_str(row, "order_id")
                if cell_str(row, "product") and not product_hint:
                    product_hint = cell_str(row, "product")
                if cell_str(row, "check_item") and not topic_hint:
                    topic_hint = cell_str(row, "check_item")
                role = _detect_sender_role(sender_val, store_name)
                raw_sender = (
                    buyer_col_val
                    if role == "buyer" and buyer_col_val
                    else (seller_col_val if role == "seller" and seller_col_val else sender_val)
                )
                chunk.append(
                    {
                        "role": role,
                        "raw_sender": raw_sender,
                        "time": _normalize_timestamp_str(cell_str(row, "started_at")),
                        "text": msg_text,
                    }
                )
            if chunk:
                combined_raw = "\n".join(m["text"] for m in chunk)
                extracted_oid = _extract_order_external_id(order_raw) or _extract_order_external_id(combined_raw)
                groups.append(
                    {
                        "raw_buyer": buyer_raw,
                        "raw_text": combined_raw,
                        "order_external_id": extracted_oid,
                        "product_hint": product_hint,
                        "topic_hint": topic_hint,
                        "started_at_hint": chunk[0]["time"],
                        "raw_messages": chunk,
                    }
                )
        if groups:
            return groups

    session_groups: list[dict[str, Any]] = []
    for row in records:
        direct_messages = row.get("messages")
        buyer_raw = cell_str(row, "buyer_nick") or str(row.get("buyer_nick") or row.get("会员名称") or "").strip()
        seller_raw = cell_str(row, "seller_nick") or str(row.get("seller_nick") or row.get("接待客服") or "").strip()
        order_cell = cell_str(row, "order_id") or str(row.get("order_external_id") or row.get("order_id") or row.get("订单编号") or "").strip()
        product_hint = cell_str(row, "product") or str(row.get("product") or row.get("商品名称") or "").strip()
        topic_hint = cell_str(row, "check_item") or str(row.get("check_item") or row.get("检出项") or "").strip()
        time_hint = _normalize_timestamp_str(
            cell_str(row, "started_at") or str(row.get("started_at") or row.get("会话时间") or "").strip()
        )

        if isinstance(direct_messages, list) and direct_messages and isinstance(direct_messages[0], dict):
            chunk = []
            for entry in direct_messages:
                msg_body = str(entry.get("text") or entry.get("content") or "").strip()
                if not msg_body:
                    continue
                sender_name = str(entry.get("sender") or entry.get("role") or "").strip()
                explicit_role = str(entry.get("role") or "").strip().lower()
                role = (
                    explicit_role
                    if explicit_role in ("buyer", "seller", "system")
                    else _detect_sender_role(sender_name, store_name)
                )
                chunk.append(
                    {
                        "role": role,
                        "raw_sender": sender_name or (buyer_raw if role == "buyer" else seller_raw),
                        "time": _normalize_timestamp_str(str(entry.get("time") or time_hint)),
                        "text": msg_body,
                    }
                )
            if chunk:
                combined_raw = "\n".join(m["text"] for m in chunk)
                extracted_oid = _extract_order_external_id(order_cell) or _extract_order_external_id(combined_raw)
                session_groups.append(
                    {
                        "raw_buyer": buyer_raw,
                        "raw_text": combined_raw,
                        "order_external_id": extracted_oid,
                        "product_hint": product_hint,
                        "topic_hint": topic_hint,
                        "started_at_hint": time_hint,
                        "raw_messages": chunk,
                    }
                )
            continue

        chat_body = cell_str(row, "chat_text")
        if not chat_body:
            fallback_parts = [
                str(val).strip()
                for key, val in row.items()
                if val is not None
                and str(val).strip()
                and key not in {mapping.get("buyer_nick"), mapping.get("order_id"), mapping.get("seller_nick"), mapping.get("started_at")}
            ]
            chat_body = "\n".join(fallback_parts)
        if not chat_body.strip():
            continue

        sub_groups = _parse_dialogue_lines(
            chat_body,
            store_name=store_name,
            default_buyer=buyer_raw,
            default_seller=seller_raw,
            default_time=time_hint,
            allow_multi_split=False,
        )
        for grp in sub_groups:
            grp["raw_buyer"] = grp["raw_buyer"] or buyer_raw
            grp["order_external_id"] = (
                _extract_order_external_id(order_cell)
                or grp["order_external_id"]
                or _extract_order_external_id(chat_body)
            )
            grp["product_hint"] = product_hint
            grp["topic_hint"] = topic_hint
            grp["started_at_hint"] = grp["started_at_hint"] or time_hint
            session_groups.append(grp)

    return session_groups


def _parse_any_chat_input(
    text_or_bytes: str | bytes,
    filename: str = "chat-paste.txt",
    store_name: str = "",
) -> list[dict[str, Any]]:
    text, raw_bytes = _decode_input_bytes(text_or_bytes)
    lower_name = (filename or "").lower()

    if lower_name.endswith(".xlsx") or (raw_bytes is not None and raw_bytes.startswith(b"PK\x03\x04")):
        if raw_bytes is None:
            raw_bytes = text.encode("utf-8")
        headers, xlsx_rows = _read_xlsx_rows(raw_bytes)
        groups = _records_to_session_groups(xlsx_rows, headers, store_name=store_name)
        if not groups:
            raise ImportProblem("XLSX 文件中未识别到有效聊天内容")
        return groups

    if not text or not text.strip():
        raise ImportProblem("聊天内容为空，请先粘贴千牛对话文本或上传质检明细文件")

    json_records = _try_parse_json_records(text, filename)
    if json_records is not None:
        all_keys: list[str] = []
        for rec in json_records:
            for key in rec.keys():
                if key not in all_keys:
                    all_keys.append(str(key))
        groups = _records_to_session_groups(json_records, all_keys, store_name=store_name)
        if not groups:
            raise ImportProblem("JSON 内容中未识别到有效会话文本")
        return groups

    md_table = _try_parse_markdown_table(text)
    if md_table is not None:
        headers, md_rows = md_table
        groups = _records_to_session_groups(md_rows, headers, store_name=store_name)
        if groups:
            return groups

    delimited = _try_parse_delimited_table(text, filename)
    if delimited is not None:
        headers, table_rows = delimited
        groups = _records_to_session_groups(table_rows, headers, store_name=store_name)
        if groups:
            return groups

    groups = _parse_dialogue_lines(text, store_name=store_name, allow_multi_split=True)
    if not groups:
        raise ImportProblem("未识别到有效对话内容，请检查粘贴文本是否为空")
    return groups


def _load_order_lookup(db: Session, store_id: int, order_ids: set[str]) -> dict[str, dict[str, Any]]:
    clean_ids = {oid for oid in order_ids if oid}
    if not clean_ids:
        return {}
    rows = db.scalars(
        select(OrderItem).where(
            OrderItem.store_id == store_id,
            (OrderItem.order_external_id.in_(clean_ids)) | (OrderItem.item_external_id.in_(clean_ids)),
        )
    ).all()
    grouped: dict[str, list[OrderItem]] = defaultdict(list)
    for row in rows:
        if row.order_external_id in clean_ids:
            grouped[row.order_external_id].append(row)
        if row.item_external_id in clean_ids and row.item_external_id != row.order_external_id:
            grouped[row.item_external_id].append(row)

    lookup: dict[str, dict[str, Any]] = {}
    for oid, items in grouped.items():
        primary = items[0]
        has_payment = any(item.payment_fen is not None for item in items)
        lookup[oid] = {
            "order_external_id": primary.order_external_id,
            "item_external_id": primary.item_external_id,
            "sku": primary.sku,
            "product_title": primary.product_title,
            "quantity": sum(item.quantity for item in items),
            "payment_fen": sum(item.payment_fen or 0 for item in items) if has_payment else None,
            "paid_at": primary.paid_at,
            "status": primary.status,
            "refund_status": primary.refund_status,
        }
    return lookup


def _analyze_messages(
    messages: list[dict[str, str]],
    *,
    linked_order: dict[str, Any] | None = None,
    product_hint: str = "",
    topic_hint: str = "",
) -> dict[str, Any]:
    buyer_texts = [msg["text"] for msg in messages if msg["role"] == "buyer"]
    seller_texts = [msg["text"] for msg in messages if msg["role"] == "seller"]
    all_text = "\n".join(msg["text"] for msg in messages)
    buyer_joined = "\n".join(buyer_texts)
    combined_lower = f"{topic_hint}\n{all_text}".lower()
    buyer_lower = f"{topic_hint}\n{buyer_joined}".lower()

    product_category = "other"
    if linked_order:
        order_desc = f"{linked_order.get('sku', '')} {linked_order.get('product_title', '')}"
        product_category = classify_product_category(order_desc)
    if product_category == "other" and product_hint:
        product_category = classify_product_category(product_hint)
    if product_category == "other":
        product_category = classify_product_category(all_text)

    topic_scores: dict[str, int] = {}
    for slug, keywords in _TOPIC_KEYWORDS.items():
        score = 0
        for kw in keywords:
            kw_low = kw.lower()
            if kw_low in buyer_lower:
                score += 3
            elif kw_low in combined_lower:
                score += 1
        if score > 0:
            topic_scores[slug] = score

    if topic_scores:
        ranked_topics = sorted(
            topic_scores.keys(),
            key=lambda slug: (-topic_scores[slug], _TOPIC_PRIORITY.index(slug)),
        )
        primary_topic = ranked_topics[0]
        topics = ranked_topics
    else:
        primary_topic = "general"
        topics = ["general"]

    demand_signals: list[str] = []
    demand_source_lower = (buyer_joined or all_text).lower()
    for label, triggers in _DEMAND_SIGNAL_RULES:
        if any(trig.lower() in demand_source_lower for trig in triggers):
            demand_signals.append(label)

    has_refund_order = bool(linked_order and linked_order.get("refund_status") in ("pending", "success"))
    aftersale_problem_cues = (
        "登不上",
        "登录不上",
        "无法登录",
        "激活失败",
        "地区不支持",
        "此优惠不可用",
        "加入不了",
        "不同国家",
        "掉线",
        "封号",
        "停用",
        "失效",
        "换号",
        "报错",
    )
    delivery_cues = ("已拍", "拍下", "发货", "发我", "什么时候发", "提取卡密", "已发您", "账号已发", "改密码")

    if "refund_dispute" in topics or (has_refund_order and any(w in combined_lower for w in ("退", "售后", "不想要"))):
        stage = "refund"
    elif any(tp in topics for tp in ("warranty_replace", "login_2fa")) or any(
        cue in combined_lower for cue in aftersale_problem_cues
    ):
        stage = "aftersale"
    elif linked_order or any(cue in combined_lower for cue in delivery_cues):
        stage = "delivery"
    else:
        stage = "presale"

    urgent_cues = ("投诉", "差评", "骗子", "举报", "小二介入", "平台介入", "赶紧退", "立马退", "太坑", "急用", "！！！")
    confused_cues = ("登不上", "打不开", "报错", "失败", "怎么弄", "不会弄", "看不懂", "卡住", "为什么提示", "进不去", "激活不了")
    resolved_cues = (
        "解决了",
        "搞定了",
        "登上了",
        "登录成功",
        "激活成功",
        "进去了",
        "弄好了",
        "可以了谢谢",
        "好的谢谢",
        "谢谢老板",
        "感谢客服",
        "收到谢谢",
    )
    unresolved_negations = ("没解决", "还是不行", "还是登不上", "依然报错", "还是打不开", "没好")

    tail_buyer = " ".join(buyer_texts[-2:]).lower() if buyer_texts else ""
    is_tail_negated = any(neg in tail_buyer for neg in unresolved_negations)
    resolved = False
    if tail_buyer and not is_tail_negated:
        if any(cue in tail_buyer for cue in resolved_cues):
            resolved = True
        elif stage in ("presale", "delivery") and messages[-1]["role"] in ("seller", "buyer"):
            if any(ack in tail_buyer for ack in ("好的", "明白", "知道了", "收到", "ok")) and not any(
                uc in combined_lower for uc in urgent_cues
            ):
                resolved = True

    if resolved:
        sentiment = "resolved"
    elif any(cue in combined_lower for cue in urgent_cues):
        sentiment = "urgent"
    elif any(cue in combined_lower for cue in confused_cues) or stage in ("aftersale", "refund"):
        sentiment = "confused"
    else:
        sentiment = "neutral"

    trivial_buyer = {"在吗", "在不在", "你好", "您好", "老板", "客服", "？", "?", "1", "好的", "收到", "嗯", "哦"}
    buyer_questions: list[str] = []
    for b_txt in buyer_texts:
        for seg in b_txt.splitlines():
            cleaned_seg = re.sub(r"\s+", " ", seg).strip()
            if not cleaned_seg or cleaned_seg in trivial_buyer or cleaned_seg == REDACTED_TOKEN:
                continue
            if re.fullmatch(r"(?:订单编号|订单号)?\s*[:：]?\s*\d{15,22}", cleaned_seg):
                continue
            clipped = cleaned_seg[:120]
            if clipped not in buyer_questions:
                buyer_questions.append(clipped)
            if len(buyer_questions) >= 5:
                break
        if len(buyer_questions) >= 5:
            break

    trivial_seller = {"在的", "亲在的", "您好", "亲您好", "稍等", "稍等哈", "好的", "不客气", "嗯嗯"}
    seller_replies: list[str] = []
    for s_txt in seller_texts:
        cleaned_reply = re.sub(r"\n{2,}", "\n", s_txt).strip()
        if len(cleaned_reply) < 8 or cleaned_reply in trivial_seller or cleaned_reply == REDACTED_TOKEN:
            continue
        clipped_reply = cleaned_reply[:260]
        if clipped_reply not in seller_replies:
            seller_replies.append(clipped_reply)
        if len(seller_replies) >= 5:
            break

    summary = _build_session_summary(
        product_category=product_category,
        stage=stage,
        primary_topic=primary_topic,
        demand_signals=demand_signals,
        resolved=resolved,
        buyer_questions=buyer_questions,
        has_seller_reply=bool(seller_replies),
    )

    return {
        "product_category": product_category,
        "stage": stage,
        "primary_topic": primary_topic,
        "topics": topics,
        "demand_signals": demand_signals,
        "sentiment": sentiment,
        "resolved": resolved,
        "summary": sanitize_chat_text(summary)[:300],
        "buyer_questions": buyer_questions,
        "seller_replies": seller_replies,
    }


def _build_session_summary(
    *,
    product_category: str,
    stage: str,
    primary_topic: str,
    demand_signals: list[str],
    resolved: bool,
    buyer_questions: list[str],
    has_seller_reply: bool,
) -> str:
    cat_name = PRODUCT_CATEGORY_LABELS.get(product_category, "商品")
    topic_name = TOPIC_LABELS.get(primary_topic, "常规咨询")
    status_tail = (
        "，客服已解答并顺利解决"
        if resolved and has_seller_reply
        else ("，已解决闭环" if resolved else ("，客服已跟进指导" if has_seller_reply else "，待客服跟进处理"))
    )
    if primary_topic == "new_demand" and demand_signals:
        sig_text = "、".join(demand_signals[:2])
        return f"买家在{STAGE_LABELS.get(stage, '咨询')}中提出{sig_text}，属于潜在新品/规格需求{status_tail}"
    if primary_topic == "login_2fa":
        return f"买家反馈{cat_name}登录与2FA两步验证问题{status_tail}"
    if primary_topic == "activation_region":
        return f"买家反馈{cat_name}激活链接地区限制或优惠风控提示{status_tail}"
    if primary_topic == "family_invite":
        return f"买家咨询{cat_name}加入邀请与账号同区要求{status_tail}"
    if primary_topic == "warranty_replace":
        return f"买家反馈{cat_name}掉线或风控停用申请质保换号{status_tail}"
    if primary_topic == "refund_dispute":
        return f"买家就{cat_name}发起退款/仅退款协商{status_tail}"
    if primary_topic == "bulk_pricing":
        return f"买家咨询{cat_name}批量团购优惠与改价方案{status_tail}"
    if buyer_questions:
        short_q = buyer_questions[0][:48]
        return f"买家围绕{cat_name}咨询「{short_q}」（{topic_name}）{status_tail}"
    return f"{cat_name}{STAGE_LABELS.get(stage, '接待')}：{topic_name}{status_tail}"


def _compute_session_hash(
    buyer_nick: str,
    order_external_id: str | None,
    messages: list[dict[str, str]],
) -> str:
    canonical = {
        "buyer": buyer_nick,
        "order": order_external_id or "",
        "messages": [(msg["role"], re.sub(r"\s+", " ", msg["text"]).strip()) for msg in messages],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _merge_message_lists(
    existing_msgs: list[dict[str, str]],
    incoming_msgs: list[dict[str, str]],
) -> tuple[list[dict[str, str]], bool]:
    existing_keys = [(msg.get("role", ""), re.sub(r"\s+", " ", msg.get("text", "")).strip()) for msg in existing_msgs]
    incoming_keys = [(msg.get("role", ""), re.sub(r"\s+", " ", msg.get("text", "")).strip()) for msg in incoming_msgs]

    if incoming_keys == existing_keys:
        return existing_msgs, False

    if len(incoming_keys) > len(existing_keys) and incoming_keys[: len(existing_keys)] == existing_keys:
        return incoming_msgs, True

    if len(existing_keys) >= len(incoming_keys):
        start_limit = len(existing_keys) - len(incoming_keys) + 1
        for offset in range(start_limit):
            if existing_keys[offset : offset + len(incoming_keys)] == incoming_keys:
                return existing_msgs, False

    merged = list(existing_msgs)
    seen_pairs = set(existing_keys)
    added = False
    for msg, key_pair in zip(incoming_msgs, incoming_keys):
        if key_pair not in seen_pairs:
            merged.append(msg)
            seen_pairs.add(key_pair)
            added = True
    return merged, added


def _auto_precipitate_knowledge(
    db: Session,
    store_id: int,
    session_obj: ChatSession,
) -> bool:
    topic = session_obj.primary_topic
    if topic not in DEFAULT_KNOWLEDGE_TEMPLATES:
        return False

    existing = db.scalar(
        select(ChatKnowledgeItem)
        .where(
            ChatKnowledgeItem.store_id == store_id,
            ChatKnowledgeItem.topic == topic,
        )
        .order_by(ChatKnowledgeItem.is_pinned.desc(), ChatKnowledgeItem.id.asc())
    )
    if existing is not None:
        existing.source_count = (existing.source_count or 0) + 1
        existing.updated_at = now_iso()
        return True

    template = DEFAULT_KNOWLEDGE_TEMPLATES[topic]
    trigger_q = (
        session_obj.buyer_questions[0]
        if session_obj.buyer_questions
        else template["trigger_question"]
    )

    best_seller_reply = ""
    for candidate_reply in session_obj.seller_replies or []:
        cleaned = sanitize_chat_text(candidate_reply)
        if REDACTED_TOKEN in cleaned or len(cleaned) < 28:
            continue
        best_seller_reply = cleaned
        break

    standard_reply = template["standard_reply"]
    if best_seller_reply and best_seller_reply not in standard_reply:
        standard_reply = f"{template['standard_reply']}\n\n【本店实战补充】{best_seller_reply}"

    card = ChatKnowledgeItem(
        store_id=store_id,
        topic=topic,
        product_category=session_obj.product_category if session_obj.product_category != "other" else "all",
        title=sanitize_chat_text(template["title"])[:160],
        trigger_question=sanitize_chat_text(trigger_q)[:300],
        standard_reply=sanitize_chat_text(standard_reply),
        source_count=1,
        is_pinned=False,
        updated_at=now_iso(),
    )
    db.add(card)
    db.flush()
    return True


def _serialize_session(
    session_obj: ChatSession,
    order_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    oid = session_obj.order_external_id or ""
    linked_order = order_lookup.get(oid) if oid else None
    return {
        "id": session_obj.id,
        "store_id": session_obj.store_id,
        "session_hash": session_obj.session_hash,
        "buyer_nick": session_obj.buyer_nick,
        "order_external_id": session_obj.order_external_id,
        "product_category": session_obj.product_category,
        "product_category_label": PRODUCT_CATEGORY_LABELS.get(session_obj.product_category, "其他商品"),
        "stage": session_obj.stage,
        "stage_label": STAGE_LABELS.get(session_obj.stage, session_obj.stage),
        "primary_topic": session_obj.primary_topic,
        "primary_topic_label": TOPIC_LABELS.get(session_obj.primary_topic, session_obj.primary_topic),
        "topics": list(session_obj.topics or []),
        "topic_labels": [TOPIC_LABELS.get(tp, tp) for tp in (session_obj.topics or [])],
        "demand_signals": list(session_obj.demand_signals or []),
        "sentiment": session_obj.sentiment,
        "sentiment_label": SENTIMENT_LABELS.get(session_obj.sentiment, session_obj.sentiment),
        "resolved": bool(session_obj.resolved),
        "summary": session_obj.summary,
        "buyer_questions": list(session_obj.buyer_questions or []),
        "seller_replies": list(session_obj.seller_replies or []),
        "messages": list(session_obj.messages or []),
        "message_count": session_obj.message_count,
        "started_at": session_obj.started_at,
        "last_message_at": session_obj.last_message_at,
        "source": session_obj.source,
        "created_at": session_obj.created_at,
        "linked_order": linked_order,
    }


def _serialize_knowledge_item(item: ChatKnowledgeItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "store_id": item.store_id,
        "topic": item.topic,
        "topic_label": TOPIC_LABELS.get(item.topic, item.topic),
        "product_category": item.product_category,
        "product_category_label": KNOWLEDGE_CATEGORY_LABELS.get(item.product_category, item.product_category),
        "title": item.title,
        "trigger_question": item.trigger_question,
        "standard_reply": item.standard_reply,
        "source_count": item.source_count,
        "is_pinned": bool(item.is_pinned),
        "updated_at": item.updated_at,
    }


def _safe_model_endpoint() -> tuple[str, str, str] | None:
    base = os.environ.get("MULTISTORE_MODEL_URL", "").strip()
    key = os.environ.get("MULTISTORE_MODEL_API_KEY", "").strip()
    model = os.environ.get("MULTISTORE_MODEL_NAME", "").strip()
    if not (base and key and model):
        return None
    parsed = urlsplit(base)
    if not parsed.hostname:
        return None
    is_safe_scheme = parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    )
    if not is_safe_scheme:
        return None
    return base.rstrip("/"), key, model


def _maybe_refine_summary_with_ai(redacted_messages: list[dict[str, str]], fallback_summary: str) -> str:
    endpoint = _safe_model_endpoint()
    if endpoint is None:
        return fallback_summary
    base_url, api_key, model_name = endpoint
    transcript = "\n".join(
        f"{'买家' if msg['role'] == 'buyer' else '客服'}：{msg['text'][:160]}"
        for msg in redacted_messages[:12]
    )
    try:
        with httpx.Client(timeout=httpx.Timeout(12, connect=4), follow_redirects=False, trust_env=False) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_name,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是淘宝虚拟数字店铺客服主管。请用一句不超过60字的中文总结该段脱敏对话的核心问题与处理结果，不输出多余标点或解释。",
                        },
                        {"role": "user", "content": transcript},
                    ],
                },
            )
            resp.raise_for_status()
            content = str(resp.json()["choices"][0]["message"]["content"] or "").strip()
            if content:
                return sanitize_chat_text(content)[:300]
    except Exception:
        return fallback_summary
    return fallback_summary


def import_chats(
    db: Session,
    store_id: int,
    text_or_bytes: str | bytes,
    filename: str = "chat-paste.txt",
    source: str = "paste",
    use_ai: bool = False,
) -> dict[str, Any]:
    store = db.get(Store, store_id)
    store_name = store.name if store else ""
    parsed_groups = _parse_any_chat_input(text_or_bytes, filename=filename, store_name=store_name)

    serialize_sqlite_writes(db)
    candidate_order_ids = {grp["order_external_id"] for grp in parsed_groups if grp.get("order_external_id")}
    order_lookup = _load_order_lookup(db, store_id, candidate_order_ids)

    inserted_count = 0
    updated_count = 0
    duplicate_count = 0
    knowledge_touched = 0
    affected_sessions: list[ChatSession] = []

    for grp in parsed_groups:
        masked_buyer = mask_buyer_nick(grp.get("raw_buyer", ""))
        order_ext_id = grp.get("order_external_id")
        sanitized_msgs: list[dict[str, str]] = []
        for raw_msg in grp["raw_messages"]:
            role = raw_msg["role"]
            if role == "buyer":
                sender_display = masked_buyer
            elif role == "seller":
                sender_display = mask_seller_nick(raw_msg["raw_sender"])
            else:
                sender_display = "系统提示"
            clean_body = sanitize_chat_text(raw_msg["text"])
            if not clean_body:
                continue
            sanitized_msgs.append(
                {
                    "role": role,
                    "sender": sender_display,
                    "time": raw_msg.get("time", ""),
                    "text": clean_body,
                }
            )
        if not sanitized_msgs:
            continue

        sess_hash = _compute_session_hash(masked_buyer, order_ext_id, sanitized_msgs)
        exact_dup = db.scalar(
            select(ChatSession).where(
                ChatSession.store_id == store_id,
                ChatSession.session_hash == sess_hash,
            )
        )
        if exact_dup is not None:
            duplicate_count += 1
            if exact_dup not in affected_sessions:
                affected_sessions.append(exact_dup)
            continue

        merge_target: ChatSession | None = None
        if order_ext_id:
            order_candidates = db.scalars(
                select(ChatSession)
                .where(
                    ChatSession.store_id == store_id,
                    ChatSession.order_external_id == order_ext_id,
                )
                .order_by(ChatSession.id.desc())
            ).all()
            for cand in order_candidates:
                if (
                    cand.buyer_nick == masked_buyer
                    or cand.buyer_nick in ("", "匿名买家")
                    or masked_buyer in ("", "匿名买家")
                ):
                    merge_target = cand
                    break
        elif masked_buyer not in ("", "匿名买家"):
            buyer_candidates = db.scalars(
                select(ChatSession)
                .where(
                    ChatSession.store_id == store_id,
                    ChatSession.buyer_nick == masked_buyer,
                    ChatSession.order_external_id.is_(None),
                )
                .order_by(ChatSession.id.desc())
            ).all()
            incoming_pairs = [
                (msg["role"], re.sub(r"\s+", " ", msg["text"]).strip()) for msg in sanitized_msgs
            ]
            for cand in buyer_candidates:
                existing_pairs = [
                    (m.get("role", ""), re.sub(r"\s+", " ", m.get("text", "")).strip())
                    for m in (cand.messages or [])
                ]
                if existing_pairs and (
                    incoming_pairs[: len(existing_pairs)] == existing_pairs
                    or existing_pairs[: len(incoming_pairs)] == incoming_pairs
                ):
                    merge_target = cand
                    break

        linked_order = order_lookup.get(order_ext_id) if order_ext_id else None
        timestamps = [msg["time"] for msg in sanitized_msgs if msg.get("time")]
        fallback_ts = grp.get("started_at_hint") or ""

        if merge_target is not None:
            merged_msgs, has_new = _merge_message_lists(list(merge_target.messages or []), sanitized_msgs)
            if not has_new:
                duplicate_count += 1
                if merge_target not in affected_sessions:
                    affected_sessions.append(merge_target)
                continue

            effective_buyer = (
                masked_buyer
                if merge_target.buyer_nick in ("", "匿名买家") and masked_buyer not in ("", "匿名买家")
                else merge_target.buyer_nick
            )
            merged_hash = _compute_session_hash(effective_buyer, order_ext_id or merge_target.order_external_id, merged_msgs)
            hash_conflict = db.scalar(
                select(ChatSession).where(
                    ChatSession.store_id == store_id,
                    ChatSession.session_hash == merged_hash,
                    ChatSession.id != merge_target.id,
                )
            )
            if hash_conflict is not None:
                duplicate_count += 1
                if hash_conflict not in affected_sessions:
                    affected_sessions.append(hash_conflict)
                continue

            analysis = _analyze_messages(
                merged_msgs,
                linked_order=linked_order,
                product_hint=grp.get("product_hint", ""),
                topic_hint=grp.get("topic_hint", ""),
            )
            if use_ai:
                analysis["summary"] = _maybe_refine_summary_with_ai(merged_msgs, analysis["summary"])

            merged_times = [msg.get("time", "") for msg in merged_msgs if msg.get("time")]
            merge_target.buyer_nick = effective_buyer
            merge_target.session_hash = merged_hash
            merge_target.order_external_id = order_ext_id or merge_target.order_external_id
            merge_target.product_category = analysis["product_category"]
            merge_target.stage = analysis["stage"]
            merge_target.primary_topic = analysis["primary_topic"]
            merge_target.topics = analysis["topics"]
            merge_target.demand_signals = analysis["demand_signals"]
            merge_target.sentiment = analysis["sentiment"]
            merge_target.resolved = analysis["resolved"]
            merge_target.summary = analysis["summary"]
            merge_target.buyer_questions = analysis["buyer_questions"]
            merge_target.seller_replies = analysis["seller_replies"]
            merge_target.messages = merged_msgs
            merge_target.message_count = len(merged_msgs)
            if merged_times:
                merge_target.started_at = merge_target.started_at or merged_times[0]
                merge_target.last_message_at = merged_times[-1]
            updated_count += 1
            if merge_target not in affected_sessions:
                affected_sessions.append(merge_target)
            continue

        analysis = _analyze_messages(
            sanitized_msgs,
            linked_order=linked_order,
            product_hint=grp.get("product_hint", ""),
            topic_hint=grp.get("topic_hint", ""),
        )
        if use_ai:
            analysis["summary"] = _maybe_refine_summary_with_ai(sanitized_msgs, analysis["summary"])

        started_val = timestamps[0] if timestamps else fallback_ts
        last_val = timestamps[-1] if timestamps else (fallback_ts or now_iso()[:19].replace("T", " "))

        new_session = ChatSession(
            store_id=store_id,
            session_hash=sess_hash,
            buyer_nick=masked_buyer,
            order_external_id=order_ext_id,
            product_category=analysis["product_category"],
            stage=analysis["stage"],
            primary_topic=analysis["primary_topic"],
            topics=analysis["topics"],
            demand_signals=analysis["demand_signals"],
            sentiment=analysis["sentiment"],
            resolved=analysis["resolved"],
            summary=analysis["summary"],
            buyer_questions=analysis["buyer_questions"],
            seller_replies=analysis["seller_replies"],
            messages=sanitized_msgs,
            message_count=len(sanitized_msgs),
            started_at=started_val[:30],
            last_message_at=last_val[:30],
            source=(source or "paste")[:40],
            created_at=now_iso(),
        )
        db.add(new_session)
        db.flush()
        if _auto_precipitate_knowledge(db, store_id, new_session):
            knowledge_touched += 1
        inserted_count += 1
        affected_sessions.append(new_session)

    if inserted_count == 0 and updated_count == 0 and duplicate_count == 0:
        raise ImportProblem("未提取到可保存的有效聊天消息")

    db.commit()
    all_oids = {sess.order_external_id for sess in affected_sessions if sess.order_external_id}
    final_order_lookup = _load_order_lookup(db, store_id, all_oids)
    return {
        "status": "ok",
        "imported_count": inserted_count,
        "inserted": inserted_count,
        "updated_count": updated_count,
        "duplicate_count": duplicate_count,
        "knowledge_updated": knowledge_touched,
        "sessions": [_serialize_session(sess, final_order_lookup) for sess in affected_sessions],
    }


def _build_readonly_qianniu_script() -> str:
    return f"""
const SENTINEL = {json.dumps(_BROWSER_SENTINEL)};
try {{
  const spaces = await listTaskSpaces();
  const pages = [];
  let skippedUserSpaces = 0;
  for (const entry of spaces) {{
    if (!entry || entry.ownership === "user" || entry.ownership === "agentDelegatedToUser") {{
      skippedUserSpaces += 1;
      continue;
    }}
    const task = await taskSpace(entry.id);
    const managed = await task.pages();
    const tabs = await task.tabs();
    const tabMap = new Map(tabs.filter((item) => item && item.label).map((item) => [item.label, item]));
    for (const pg of managed) {{
      const meta = tabMap.get(pg.label) || {{}};
      const url = String(meta.url || "");
      const title = String(meta.title || "");
      const combined = (url + " " + title).toLowerCase();
      const isQianniu = (
        combined.includes("qianniu") ||
        combined.includes("myseller.taobao") ||
        combined.includes("qn.taobao") ||
        combined.includes("im.taobao") ||
        combined.includes("wangwang") ||
        combined.includes("千牛") ||
        combined.includes("质检") ||
        combined.includes("接待")
      );
      if (!isQianniu) continue;
      const text = await pg.evaluate(() => (document.body ? document.body.innerText : ""));
      if (text && text.trim()) {{
        pages.push({{ url, title, text }});
      }}
    }}
  }}
  console.log(SENTINEL + JSON.stringify({{ ok: true, pages, skippedUserSpaces }}));
}} catch (err) {{
  console.log(SENTINEL + JSON.stringify({{ ok: false, error: String(err) }}));
}}
"""


def _run_readonly_browser_probe(script_body: str, *, timeout: int = 25) -> dict[str, Any]:
    ego_exe = find_ego_browser()
    temp_dir = Path(tempfile.gettempdir()) / "multistore-ego"
    temp_dir.mkdir(parents=True, exist_ok=True)
    script_path = temp_dir / f"qn-sync-{uuid4().hex}.mjs"
    script_path.write_text(script_body, encoding="utf-8")
    try:
        proc = subprocess.run(
            [ego_exe, "nodejs", "-e", f"await import('{script_path.as_uri()}')"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    for line in reversed(combined.splitlines()):
        if line.startswith(_BROWSER_SENTINEL):
            return json.loads(line[len(_BROWSER_SENTINEL) :])
    return {"ok": False, "pages": []}


def sync_chats_from_browser(
    db: Session,
    store_id: int,
    *,
    runner=_run_readonly_browser_probe,
) -> dict[str, Any]:
    guide_payload = {
        "status": "guide",
        "imported": 0,
        "imported_count": 0,
        "updated_count": 0,
        "duplicate_count": 0,
        "message": BROWSER_GUIDE_MESSAGE,
    }
    try:
        probe = runner(_build_readonly_qianniu_script())
    except Exception:
        return guide_payload

    if not isinstance(probe, dict) or not probe.get("ok"):
        return guide_payload

    pages = probe.get("pages") or []
    if not pages:
        return guide_payload

    total_imported = 0
    total_updated = 0
    total_duplicates = 0
    collected_sessions: list[dict[str, Any]] = []

    for page_entry in pages:
        page_text = str(page_entry.get("text") or "").strip()
        if not page_text:
            continue
        title = str(page_entry.get("title") or "")
        inferred_source = "quality_desk" if "质检" in title else "ego_browser"
        try:
            res = import_chats(
                db,
                store_id,
                page_text,
                filename="browser-sync.txt",
                source=inferred_source,
            )
        except ImportProblem:
            continue
        total_imported += res["imported_count"]
        total_updated += res["updated_count"]
        total_duplicates += res["duplicate_count"]
        collected_sessions.extend(res["sessions"])

    if total_imported == 0 and total_updated == 0 and total_duplicates == 0:
        return guide_payload

    return {
        "status": "ok",
        "imported": total_imported,
        "imported_count": total_imported,
        "updated_count": total_updated,
        "duplicate_count": total_duplicates,
        "sessions": collected_sessions,
        "message": f"已从浏览器只读同步 {total_imported} 段新会话（合并更新 {total_updated} 段，跳过重复 {total_duplicates} 段）。",
    }


def _session_in_date_window(sess: ChatSession, start: str, end: str) -> bool:
    ref_date = (sess.last_message_at or sess.started_at or sess.created_at or "")[:10]
    if not ref_date:
        return True
    if start and len(start) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", ref_date):
        if ref_date < start[:10]:
            return False
    if end and len(end) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", ref_date):
        if ref_date > end[:10]:
            return False
    return True


def _parse_resolved_filter(resolved: str | bool | None) -> bool | None:
    if isinstance(resolved, bool):
        return resolved
    val = str(resolved or "").strip().lower()
    if val in ("", "all", "none"):
        return None
    if val in ("1", "true", "yes", "resolved", "已解决"):
        return True
    if val in ("0", "false", "no", "unresolved", "pending", "未解决", "待跟进"):
        return False
    return None


def chat_overview(
    db: Session,
    store_id: int,
    category: str = "",
    stage: str = "",
    topic: str = "",
    resolved: str | bool = "",
    q: str = "",
    start: str = "",
    end: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    all_rows = db.scalars(
        select(ChatSession)
        .where(ChatSession.store_id == store_id)
        .order_by(ChatSession.last_message_at.desc(), ChatSession.id.desc())
    ).all()

    window_rows = [sess for sess in all_rows if _session_in_date_window(sess, start, end)]
    order_ids = {sess.order_external_id for sess in window_rows if sess.order_external_id}
    order_lookup = _load_order_lookup(db, store_id, order_ids)

    total_sessions = len(window_rows)
    total_messages = sum(sess.message_count or len(sess.messages or []) for sess in window_rows)
    linked_order_sessions = sum(
        1 for sess in window_rows if sess.order_external_id and sess.order_external_id in order_lookup
    )
    pending_sessions = sum(1 for sess in window_rows if not sess.resolved or (sess.sentiment == "urgent" and not sess.resolved))
    urgent_sessions = sum(1 for sess in window_rows if sess.sentiment == "urgent" and not sess.resolved)
    resolved_sessions = sum(1 for sess in window_rows if sess.resolved)

    by_category: dict[str, int] = {slug: 0 for slug in PRODUCT_CATEGORY_LABELS}
    by_stage: dict[str, int] = {slug: 0 for slug in STAGE_LABELS}
    topic_counter: Counter[str] = Counter()
    topic_unresolved: Counter[str] = Counter()
    demand_counter: Counter[str] = Counter()
    demand_examples: dict[str, list[str]] = defaultdict(list)

    for sess in window_rows:
        cat_key = sess.product_category if sess.product_category in by_category else "other"
        by_category[cat_key] += 1
        stg_key = sess.stage if sess.stage in by_stage else "presale"
        by_stage[stg_key] += 1

        sess_topics = set(sess.topics or [sess.primary_topic])
        sess_topics.add(sess.primary_topic)
        if len(sess_topics) > 1 and "general" in sess_topics:
            sess_topics.discard("general")
        for tp in sess_topics:
            topic_counter[tp] += 1
            if not sess.resolved:
                topic_unresolved[tp] += 1

        for sig in sess.demand_signals or []:
            demand_counter[sig] += 1
            sample_text = (sess.buyer_questions[0] if sess.buyer_questions else sess.summary) or sess.summary
            if sample_text and sample_text not in demand_examples[sig] and len(demand_examples[sig]) < 3:
                demand_examples[sig].append(sample_text)

    topic_ranking = [
        {
            "topic": tp,
            "label": TOPIC_LABELS.get(tp, tp),
            "count": count,
            "ratio_pct": round(count * 100.0 / total_sessions, 1) if total_sessions else 0.0,
            "unresolved_count": topic_unresolved[tp],
        }
        for tp, count in sorted(topic_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    demand_ranking = [
        {
            "demand": sig,
            "label": sig,
            "count": count,
            "ratio_pct": round(count * 100.0 / total_sessions, 1) if total_sessions else 0.0,
            "examples": demand_examples[sig],
            "sample_summary": demand_examples[sig][0] if demand_examples[sig] else "",
        }
        for sig, count in sorted(demand_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    knowledge_rows = db.scalars(
        select(ChatKnowledgeItem)
        .where(ChatKnowledgeItem.store_id == store_id)
        .order_by(
            ChatKnowledgeItem.is_pinned.desc(),
            ChatKnowledgeItem.source_count.desc(),
            ChatKnowledgeItem.updated_at.desc(),
            ChatKnowledgeItem.id.desc(),
        )
    ).all()
    knowledge_items = [_serialize_knowledge_item(item) for item in knowledge_rows]

    cat_filter = (category or "").strip()
    stage_filter = (stage or "").strip()
    topic_filter = (topic or "").strip()
    resolved_filter = _parse_resolved_filter(resolved)
    query_low = (q or "").strip().lower()

    filtered_rows: list[ChatSession] = []
    for sess in window_rows:
        if cat_filter and cat_filter != "all" and sess.product_category != cat_filter:
            continue
        if stage_filter and stage_filter != "all" and sess.stage != stage_filter:
            continue
        if topic_filter and topic_filter != "all":
            if sess.primary_topic != topic_filter and topic_filter not in (sess.topics or []):
                continue
        if resolved_filter is not None and bool(sess.resolved) != resolved_filter:
            continue
        if query_low:
            linked = order_lookup.get(sess.order_external_id or "") or {}
            searchable_blob = " ".join(
                [
                    sess.buyer_nick or "",
                    sess.order_external_id or "",
                    sess.summary or "",
                    TOPIC_LABELS.get(sess.primary_topic, ""),
                    PRODUCT_CATEGORY_LABELS.get(sess.product_category, ""),
                    " ".join(sess.demand_signals or []),
                    " ".join(sess.buyer_questions or []),
                    " ".join(msg.get("text", "") for msg in (sess.messages or [])),
                    str(linked.get("sku") or ""),
                    str(linked.get("product_title") or ""),
                ]
            ).lower()
            if query_low not in searchable_blob:
                continue
        filtered_rows.append(sess)

    capped_limit = max(1, min(int(limit or 100), 500))
    serialized_sessions = [_serialize_session(sess, order_lookup) for sess in filtered_rows[:capped_limit]]

    return {
        "counts": {
            "total_sessions": total_sessions,
            "filtered_sessions": len(filtered_rows),
            "total_messages": total_messages,
            "linked_order_sessions": linked_order_sessions,
            "pending_sessions": pending_sessions,
            "unresolved_sessions": pending_sessions,
            "resolved_sessions": resolved_sessions,
            "urgent_sessions": urgent_sessions,
            "knowledge_count": len(knowledge_items),
            "by_category": by_category,
            "by_stage": by_stage,
        },
        "topic_ranking": topic_ranking,
        "demand_ranking": demand_ranking,
        "knowledge_items": knowledge_items,
        "sessions": serialized_sessions,
        "labels": {
            "categories": PRODUCT_CATEGORY_LABELS,
            "stages": STAGE_LABELS,
            "topics": TOPIC_LABELS,
            "sentiments": SENTIMENT_LABELS,
        },
    }


def upsert_knowledge_item(
    db: Session,
    store_id: int,
    payload: Any | None = None,
    *,
    item_id: int | None = None,
    topic: str | None = None,
    product_category: str | None = None,
    title: str | None = None,
    trigger_question: str | None = None,
    standard_reply: str | None = None,
    is_pinned: bool | None = None,
) -> dict[str, Any]:
    if payload is not None:
        getter = payload.get if isinstance(payload, dict) else lambda key, default=None: getattr(payload, key, default)
        if item_id is None:
            item_id = getter("id") or getter("item_id")
        if topic is None:
            topic = getter("topic")
        if product_category is None:
            product_category = getter("product_category")
        if title is None:
            title = getter("title")
        if trigger_question is None:
            trigger_question = getter("trigger_question")
        if standard_reply is None:
            standard_reply = getter("standard_reply")
        if is_pinned is None:
            is_pinned = getter("is_pinned")

    if item_id is not None:
        existing = db.scalar(
            select(ChatKnowledgeItem).where(
                ChatKnowledgeItem.store_id == store_id,
                ChatKnowledgeItem.id == int(item_id),
            )
        )
        if existing is None:
            raise ImportProblem("未找到该客服知识卡")
        if topic is not None and str(topic).strip():
            existing.topic = str(topic).strip()[:50]
        if product_category is not None and str(product_category).strip():
            existing.product_category = str(product_category).strip()[:40]
        if title is not None:
            clean_title = sanitize_chat_text(title).strip()
            if not clean_title:
                raise ImportProblem("话术标题不能为空")
            existing.title = clean_title[:160]
        if trigger_question is not None:
            existing.trigger_question = sanitize_chat_text(trigger_question).strip()[:300]
        if standard_reply is not None:
            clean_reply = sanitize_chat_text(standard_reply).strip()
            if not clean_reply:
                raise ImportProblem("标准回复话术不能为空")
            existing.standard_reply = clean_reply
        if is_pinned is not None:
            existing.is_pinned = bool(is_pinned)
        existing.updated_at = now_iso()
        db.commit()
        return _serialize_knowledge_item(existing)

    clean_title = sanitize_chat_text(title or "").strip()
    clean_reply = sanitize_chat_text(standard_reply or "").strip()
    if not clean_title or not clean_reply:
        raise ImportProblem("新建客服知识卡必须填写标题和标准回复话术")
    clean_topic = (topic or "general").strip()[:50] or "general"
    clean_cat = (product_category or "all").strip()[:40] or "all"
    clean_trigger = sanitize_chat_text(trigger_question or "").strip()[:300]

    card = ChatKnowledgeItem(
        store_id=store_id,
        topic=clean_topic,
        product_category=clean_cat,
        title=clean_title[:160],
        trigger_question=clean_trigger,
        standard_reply=clean_reply,
        source_count=1,
        is_pinned=bool(is_pinned) if is_pinned is not None else False,
        updated_at=now_iso(),
    )
    db.add(card)
    db.commit()
    return _serialize_knowledge_item(card)


def delete_knowledge_item(db: Session, store_id: int, item_id: int) -> dict[str, Any]:
    card = db.scalar(
        select(ChatKnowledgeItem).where(
            ChatKnowledgeItem.store_id == store_id,
            ChatKnowledgeItem.id == int(item_id),
        )
    )
    if card is None:
        raise ImportProblem("未找到该客服知识卡")
    db.delete(card)
    db.commit()
    return {"deleted": True, "id": int(item_id)}


def toggle_session_resolved(
    db: Session,
    store_id: int,
    session_id: int,
    resolved: bool | None = None,
) -> dict[str, Any]:
    sess = db.scalar(
        select(ChatSession).where(
            ChatSession.store_id == store_id,
            ChatSession.id == int(session_id),
        )
    )
    if sess is None:
        raise ImportProblem("未找到该聊天会话")
    target_state = (not sess.resolved) if resolved is None else bool(resolved)
    sess.resolved = target_state
    if target_state:
        sess.sentiment = "resolved"
    elif sess.sentiment == "resolved":
        sess.sentiment = "confused" if sess.stage in ("aftersale", "refund") else "neutral"
    db.commit()
    order_lookup = _load_order_lookup(
        db,
        store_id,
        {sess.order_external_id} if sess.order_external_id else set(),
    )
    return _serialize_session(sess, order_lookup)


def _match_topic_from_question(question_text: str) -> str | None:
    q_low = question_text.lower()
    for slug, keywords in _TOPIC_KEYWORDS.items():
        if any(kw.lower() in q_low for kw in keywords):
            return slug
    for slug, label in TOPIC_LABELS.items():
        if label.lower() in q_low:
            return slug
    if "登录" in q_low or "验证" in q_low:
        return "login_2fa"
    if "激活" in q_low or "地区" in q_low:
        return "activation_region"
    if "家庭" in q_low:
        return "family_invite"
    if "掉" in q_low or "换" in q_low:
        return "warranty_replace"
    if "退" in q_low:
        return "refund_dispute"
    if "改密" in q_low or "教程" in q_low:
        return "usage_tutorial"
    return None


def _extract_numeric_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def query_chat_insights(
    db: Session,
    store_id: int,
    question: str,
    start: str = "",
    end: str = "",
    *,
    use_ai: bool = True,
) -> dict[str, Any]:
    clean_q = str(question or "").strip()
    if not clean_q:
        raise ImportProblem("请输入要分析的聊天问题")

    overview = chat_overview(db, store_id, start=start, end=end, limit=200)
    counts = overview["counts"]
    total_sessions = counts["total_sessions"]

    if total_sessions == 0:
        empty_msg = "当前筛选范围内共有 0 段千牛聊天记录，请先粘贴千牛接待对话或导入质检明细表后再发起智能分析。"
        return {
            "question": clean_q,
            "answer": empty_msg,
            "deterministic_summary": empty_msg,
            "output_mode": "deterministic",
            "counts": counts,
            "topic_ranking": [],
            "demand_ranking": [],
            "recommended_replies": [],
            "start": start,
            "end": end,
        }

    topic_ranking = overview["topic_ranking"]
    demand_ranking = overview["demand_ranking"]
    knowledge_items = overview["knowledge_items"]
    sessions = overview["sessions"]
    q_low = clean_q.lower()

    matched_topic = _match_topic_from_question(clean_q)
    recommended_replies: list[dict[str, Any]] = []
    if matched_topic:
        recommended_replies = [item for item in knowledge_items if item["topic"] == matched_topic]
        if not recommended_replies and matched_topic in DEFAULT_KNOWLEDGE_TEMPLATES:
            tpl = DEFAULT_KNOWLEDGE_TEMPLATES[matched_topic]
            recommended_replies = [
                {
                    "id": None,
                    "topic": matched_topic,
                    "topic_label": TOPIC_LABELS.get(matched_topic, matched_topic),
                    "title": tpl["title"],
                    "trigger_question": tpl["trigger_question"],
                    "standard_reply": tpl["standard_reply"],
                    "source_count": 0,
                    "is_pinned": False,
                }
            ]
    elif knowledge_items:
        recommended_replies = knowledge_items[:3]

    asks_reply_script = any(word in q_low for word in ("话术", "标准回复", "怎么回", "回复模板", "客服回复", "模板"))
    asks_demand = any(word in q_low for word in ("选品", "新需求", "新品", "需求", "扩品", "进货", "chatgpt", "claude", "团购"))
    asks_aftersale_refund = any(word in q_low for word in ("退款", "售后", "为什么退", "纠纷", "掉线", "未解决", "待跟进"))

    lines: list[str] = [
        f"统计概览：共整理 {total_sessions} 段会话（{counts['total_messages']} 条消息），"
        f"已关联本店订单 {counts['linked_order_sessions']} 段，待跟进未解决 {counts['pending_sessions']} 段。"
    ]

    if asks_reply_script and recommended_replies:
        top_card = recommended_replies[0]
        lines.append(f"推荐标准话术{top_card['title']}：\n{top_card['standard_reply']}")
    elif asks_demand:
        if demand_ranking:
            demand_parts = [
                f"{item['label']}（{item['count']} 段，占比 {item['ratio_pct']}%）"
                for item in demand_ranking[:5]
            ]
            lines.append("挖掘到的客户需求信号排行：" + "；".join(demand_parts) + "。")
        else:
            lines.append("当前会话暂未检出明显跨品类选品需求信号。")
        if topic_ranking:
            top_tp = topic_ranking[0]
            lines.append(f"同期最高频咨询话题为「{top_tp['label']}」（{top_tp['count']} 段，占 {top_tp['ratio_pct']}%）。")
    elif asks_aftersale_refund:
        aftersale_cnt = counts["by_stage"].get("aftersale", 0)
        refund_cnt = counts["by_stage"].get("refund", 0)
        trouble_sessions = [sess for sess in sessions if sess["stage"] in ("aftersale", "refund")]
        trouble_counter: Counter[str] = Counter(sess["primary_topic"] for sess in trouble_sessions)
        reason_parts = [
            f"{TOPIC_LABELS.get(tp, tp)} {cnt} 段"
            for tp, cnt in trouble_counter.most_common(4)
        ]
        lines.append(
            f"售后与退款分布：售后排障 {aftersale_cnt} 段，退款纠纷 {refund_cnt} 段，"
            f"其中紧急情绪未闭环 {counts['urgent_sessions']} 段。"
        )
        if reason_parts:
            lines.append("主要触发原因：" + "、".join(reason_parts) + "。")
    else:
        if topic_ranking:
            tp_parts = [
                f"{item['label']} {item['count']} 段（{item['ratio_pct']}%）"
                for item in topic_ranking[:5]
            ]
            lines.append("高频痛点话题排行：" + "；".join(tp_parts) + "。")
        if demand_ranking:
            dm_parts = [f"{item['label']} {item['count']} 段" for item in demand_ranking[:3]]
            lines.append("客户需求信号：" + "、".join(dm_parts) + "。")

    deterministic_answer = "\n".join(lines)
    final_answer = deterministic_answer
    output_mode = "deterministic"

    endpoint = _safe_model_endpoint() if use_ai else None
    if endpoint is not None:
        base_url, api_key, model_name = endpoint
        stats_context = {
            "counts": counts,
            "top_topics": topic_ranking[:5],
            "top_demands": demand_ranking[:5],
            "recommended_replies": [
                {"title": card["title"], "standard_reply": card["standard_reply"]}
                for card in recommended_replies[:2]
            ],
            "baseline_facts": deterministic_answer,
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(18, connect=4), follow_redirects=False, trust_env=False) as client:
                resp = client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model_name,
                        "temperature": 0,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "你是淘宝虚拟数字商品店铺的AI运营分析顾问。请仅依据给定的脱敏统计JSON回答店主提问。"
                                    "所有数字必须原样引用统计JSON中的数字，严禁自行编造任何未出现的数字或百分比。"
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    f"店主提问：{clean_q}\n"
                                    f"真实统计数据：{json.dumps(stats_context, ensure_ascii=False)}"
                                ),
                            },
                        ],
                    },
                )
                resp.raise_for_status()
                candidate = sanitize_chat_text(resp.json()["choices"][0]["message"]["content"] or "")
                allowed_nums = _extract_numeric_tokens(json.dumps(stats_context, ensure_ascii=False))
                cand_nums = _extract_numeric_tokens(candidate)
                if candidate and cand_nums.issubset(allowed_nums):
                    final_answer = candidate
                    output_mode = "model_enhanced"
        except Exception:
            final_answer = deterministic_answer
            output_mode = "deterministic"

    return {
        "question": clean_q,
        "answer": final_answer,
        "deterministic_summary": deterministic_answer,
        "output_mode": output_mode,
        "counts": counts,
        "topic_ranking": topic_ranking[:8],
        "demand_ranking": demand_ranking[:8],
        "recommended_replies": recommended_replies,
        "start": start,
        "end": end,
    }
