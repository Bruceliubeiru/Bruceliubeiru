const aiPlatformOptions = [
  { label: "ChatGPT", value: "chatgpt", note: "答案推荐与品牌提及" },
  { label: "豆包", value: "doubao", note: "国内主流 AI 搜索问答" },
  { label: "DeepSeek", value: "deepseek", note: "推理答案与品牌出现" },
  { label: "Kimi", value: "kimi", note: "长文本答案与信源整理" },
  { label: "腾讯元宝", value: "yuanbao", note: "微信生态 AI 可见度" },
  { label: "通义千问", value: "qwen", note: "阿里系 AI 答案覆盖" },
  { label: "Perplexity", value: "perplexity", note: "引用信源与答案位置" },
  { label: "Gemini", value: "gemini", note: "Google AI 答案可见度" },
  { label: "Claude", value: "claude", note: "海外问答与比较建议" },
  { label: "AI Overview", value: "google_ai_overviews", note: "Google 搜索摘要与引用" }
]

function platformLabel(value) {
  const option = aiPlatformOptions.find((item) => item.value === value)
  return option ? option.label : value
}

module.exports = {
  aiPlatformOptions,
  platformLabel
}
