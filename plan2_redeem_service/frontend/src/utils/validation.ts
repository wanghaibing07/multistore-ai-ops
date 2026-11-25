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

const LOCAL_PART_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9])?$/;
const DOMAIN_PATTERN =
  /^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9](?:[A-Za-z0-9-]{0,57}[A-Za-z0-9])?)$/;

export interface GmailValidationResult {
  valid: boolean;
  normalized: string;
  error?: string;
  suggestion?: string;
}

export function validateGmailInput(raw: string): GmailValidationResult {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { valid: false, normalized: "", error: "请先填一下你的邮箱" };
  }
  if (/\s/.test(trimmed)) {
    return {
      valid: false,
      normalized: trimmed.replace(/\s+/g, ""),
      error: "邮箱里不能有空格，删掉空格再试",
    };
  }
  if (/[^\x20-\x7E]/.test(trimmed)) {
    return {
      valid: false,
      normalized: trimmed,
      error: "邮箱里有中文或全角符号，切回英文输入法重填一下",
    };
  }

  const parts = trimmed.split("@");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    return {
      valid: false,
      normalized: trimmed,
      error: "邮箱格式不对，应该像 name@example.com 这样",
    };
  }

  const localPart = parts[0];
  const domainPart = parts[1].toLowerCase();
  const normalized = `${localPart}@${domainPart}`;

  if (normalized.length > 254) {
    return {
      valid: false,
      normalized,
      error: "邮箱地址太长了，请核对后再提交",
    };
  }
  if (localPart.includes("..")) {
    return {
      valid: false,
      normalized,
      error: "邮箱前面不能连着写两个点（..）",
    };
  }
  if (!LOCAL_PART_PATTERN.test(localPart)) {
    return {
      valid: false,
      normalized,
      error: "邮箱名字母或符号不太对，核对一下再提交",
    };
  }

  if (GMAIL_DOMAIN_TYPOS.has(domainPart)) {
    return {
      valid: false,
      normalized,
      error: `后缀 @${domainPart} 好像拼错了`,
      suggestion: `${localPart}@gmail.com`,
    };
  }

  if (!DOMAIN_PATTERN.test(domainPart)) {
    return {
      valid: false,
      normalized,
      error: "邮箱域名格式不对，检查一下 @ 后面的内容",
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
    return { valid: false, error: "只支持 JPG、PNG 或 WebP 图片" };
  }
  if (file.size <= 0) {
    return { valid: false, error: "这张图片是空的，换一张重新选" };
  }
  if (file.size > MAX_WARRANTY_FILE_BYTES) {
    return { valid: false, error: "图片太大了，请传 5MB 以内的截图" };
  }
  return { valid: true };
}
