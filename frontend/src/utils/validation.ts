const GMAIL_DOMAIN_TYPOS = new Set([
  "gamil.com",
  "gmial.com",
  "gnail.com",
  "gmal.com",
  "gmali.com",
  "gmaill.com",
  "gmaik.com",
  "gmil.com",
  "gimail.com",
  "gmail.con",
  "gmail.co",
  "gmail.cm",
  "gmail.cmo",
  "gmail.om",
  "gmail.comm",
  "gmail.cn",
  "gmail.com.cn",
  "gmai.com",
  "googlemail.con",
  "googlemal.com",
]);

const VALID_GMAIL_DOMAINS = new Set(["gmail.com", "googlemail.com"]);

const LOCAL_PART_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,62}[A-Za-z0-9])?$/;

export interface GmailValidationResult {
  valid: boolean;
  normalized: string;
  error?: string;
  suggestion?: string;
}

export function validateGmailInput(raw: string): GmailValidationResult {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { valid: false, normalized: "", error: "请输入 Gmail 邮箱地址" };
  }
  if (/\s/.test(trimmed)) {
    return {
      valid: false,
      normalized: trimmed.replace(/\s+/g, ""),
      error: "邮箱中不能包含空格，请检查后再输入",
    };
  }
  if (/[^\x20-\x7E]/.test(trimmed)) {
    return {
      valid: false,
      normalized: trimmed,
      error: "邮箱包含中文或全角字符，请切换英文输入法重新输入",
    };
  }

  const parts = trimmed.split("@");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    return {
      valid: false,
      normalized: trimmed,
      error: "邮箱格式不正确，示例：yourname@gmail.com",
    };
  }

  const localPart = parts[0];
  const domainPart = parts[1].toLowerCase();
  const normalized = `${localPart}@${domainPart}`;

  if (localPart.includes("..")) {
    return {
      valid: false,
      normalized,
      error: "邮箱用户名不能包含连续两个英文句点（..）",
    };
  }
  if (!LOCAL_PART_PATTERN.test(localPart)) {
    return {
      valid: false,
      normalized,
      error: "邮箱用户名格式不正确，请核对字母、数字与符号",
    };
  }

  if (GMAIL_DOMAIN_TYPOS.has(domainPart)) {
    return {
      valid: false,
      normalized,
      error: `邮箱后缀「@${domainPart}」疑似拼写错误`,
      suggestion: `${localPart}@gmail.com`,
    };
  }

  if (!VALID_GMAIL_DOMAINS.has(domainPart)) {
    return {
      valid: false,
      normalized,
      error: "Google 家庭组仅支持 @gmail.com 邮箱，请勿填写 QQ / 163 / Outlook 等其他邮箱",
    };
  }

  return { valid: true, normalized };
}

export function extractRedeemCode(rawInput: string): string {
  const trimmed = rawInput.trim();
  if (!trimmed) {
    return "";
  }

  const urlMatch = trimmed.match(/\/r\/([A-Za-z0-9_-]{4,64})/i);
  if (urlMatch && urlMatch[1]) {
    return urlMatch[1].toUpperCase();
  }

  const labelMatch = trimmed.match(/(?:兑换码|提货码|code)\s*[:：]\s*([A-Za-z0-9_-]{4,64})/i);
  if (labelMatch && labelMatch[1]) {
    return labelMatch[1].toUpperCase();
  }

  const cleaned = trimmed.replace(/\s+/g, "");
  if (/^[A-Za-z0-9_-]{4,64}$/.test(cleaned)) {
    return cleaned.toUpperCase();
  }

  const tokenMatch = trimmed.match(/[A-Za-z0-9]{2,12}-[A-Za-z0-9-]{4,48}/);
  if (tokenMatch) {
    return tokenMatch[0].toUpperCase();
  }

  return cleaned.toUpperCase();
}

const ALLOWED_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const MAX_WARRANTY_FILE_BYTES = 5 * 1024 * 1024;

export interface FileValidationResult {
  valid: boolean;
  error?: string;
}

export function validateWarrantyScreenshot(file: File | null): FileValidationResult {
  if (!file) {
    return { valid: false, error: "请上传邀请确认截图" };
  }
  if (!ALLOWED_IMAGE_TYPES.has(file.type)) {
    return { valid: false, error: "仅支持 JPG、PNG 或 WebP 格式的图片截图" };
  }
  if (file.size <= 0) {
    return { valid: false, error: "图片文件为空，请重新选择" };
  }
  if (file.size > MAX_WARRANTY_FILE_BYTES) {
    return { valid: false, error: "图片大小不能超过 5MB，请压缩后重新上传" };
  }
  return { valid: true };
}
