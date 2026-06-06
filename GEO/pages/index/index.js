const {
  auditContent,
  analyzeUrl,
  improvePackage,
  saveVersion,
  reviewVersion,
  injectVersion,
  getCmsTargets,
  createPublicationPreview,
  confirmPublication,
  retryPublication,
  verifyPublication,
  schedulePublicationVerify,
  saveFeedback,
  retestTask,
  scheduleRetest,
  getTaskDetail,
  saveExperiment,
  confirmExperiment,
  saveAttribution,
  generateReport,
  confirmReport,
  exportJson,
  getMonitoringQueries,
  generateMonitoringQueries,
  parseMonitoringSources,
  getMonitoringSummary
} = require("../../utils/api")
const { aiPlatformOptions } = require("../../utils/platforms")

const dimensionLabels = {
  semantic_clarity: "语义清晰",
  citation_readiness: "引用友好",
  faq_coverage: "FAQ 覆盖",
  comparison_readiness: "对比能力",
  authority_signal: "权威信号",
  ai_readability: "AI 可读"
}

const defaultResult = {
  task_id: "geo_demo",
  status: "completed",
  title: "示例：品牌首页 GEO 诊断",
  url: "https://example.com",
  geo_score: 64,
  max_score: 100,
  content_preview: "GEO 推广的核心，是让品牌页面更容易被 AI 理解、引用、比较和推荐。",
  breakdown: {
    semantic_clarity: 14,
    citation_readiness: 12,
    faq_coverage: 7,
    comparison_readiness: 6,
    authority_signal: 8,
    ai_readability: 15
  },
  recommendations: [
    "补充面向 AI 的业务定义",
    "增加高意图 FAQ",
    "添加竞品对比和案例数据"
  ],
  growth_plan: [
    {
      title: "补齐 AI 可引用定义",
      impact: "让 AI 能用一句话准确解释你的业务",
      action: "在首页首屏加入“我们是谁、解决什么问题、适合谁”的短定义。"
    },
    {
      title: "生成 FAQ 问答资产",
      impact: "覆盖用户会直接问 AI 的高意图问题",
      action: "围绕价格、适用人群、竞品差异、使用场景先生成 10 个问答。"
    },
    {
      title: "增加对比和证据",
      impact: "提高被 AI 推荐时的可信度和可比较性",
      action: "添加案例、数据、客户结果、竞品/替代方案对比表。"
    }
  ],
  page_summary: {
    theme: "品牌首页 GEO 诊断",
    product_type: "推广页",
    target_user: ["需要提升 AI 搜索可见度的运营和产品团队"],
    market: "Hong Kong",
    language: "zh-HK",
    current_score: 64
  },
  geo_assets: {
    entities: ["GEO", "AI Search", "FAQ", "Schema", "Content Asset"],
    keywords: ["GEO optimization", "AI search visibility", "FAQ content", "Schema markup"],
    search_intents: ["comparison", "how-to", "booking", "price"],
    use_cases: ["页面改版", "SEO/GEO 内容生产", "AI 可引用答案建设"],
    product_attributes: ["清晰定义", "FAQ 覆盖", "证据", "结构化数据"]
  },
  content_gaps: ["缺少 AI 摘要", "缺少 FAQ", "缺少 Schema 建议"],
  injection_modules: [
    {
      module_type: "hero",
      title: "Plan smarter with GEO",
      body: "用结构化内容让页面更容易被 AI 理解、引用和推荐。",
      target_position: "top hero",
      priority: "high",
      cta: "Start Analysis"
    },
    {
      module_type: "ai_summary",
      title: "AI-readable page summary",
      body: "This page helps teams turn URLs into GEO-ready content assets, including FAQ, entities, keywords, schema, and conversion suggestions.",
      target_position: "below hero",
      priority: "high"
    }
  ],
  faq_items: [
    {
      question: "What is GEO?",
      answer: "GEO helps pages become easier for AI systems to understand, summarize, cite, compare, and recommend.",
      priority: "high"
    }
  ],
  schema_suggestions: [
    {
      schema_type: "WebPage",
      validation_status: "draft",
      json: {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": "GEO URL 内容注入工作台"
      }
    }
  ],
  conversion_tips: ["首屏加入 AI 摘要", "产品卡前加入选择器", "移动端保留 sticky CTA"]
}

const defaultWorkflow = {
  status: "not_started",
  predicted_score: 76,
  score_delta: 12,
  stages: [
    { key: "diagnose", name: "诊断", status: "completed", output: "GEO 分数、内容缺口、页面资产" },
    { key: "improve", name: "AI 改进", status: "waiting", output: "改进版注入模块草稿" },
    { key: "review", name: "人工审核", status: "waiting", output: "确认可上线版本" },
    { key: "inject", name: "注入", status: "waiting", output: "CMS 字段映射 JSON" },
    { key: "retest", name: "复测", status: "waiting", output: "上线后同 URL 复测" }
  ],
  improved_modules: [],
  injection_payload: null,
  retest_plan: []
}

const pageTypeOptions = [
  { label: "交通票券", value: "transport_pass" },
  { label: "推广页", value: "landing_page" },
  { label: "攻略页", value: "guide_page" },
  { label: "竞品分析", value: "competitor_page" }
]

const languageOptions = [
  { label: "繁中", value: "zh-HK" },
  { label: "简中", value: "zh-CN" },
  { label: "英文", value: "en" }
]

const marketOptions = [
  { label: "香港", value: "Hong Kong" },
  { label: "日本", value: "Japan" },
  { label: "全球", value: "Global" }
]

const engineOptions = aiPlatformOptions

const resultTabs = [
  { label: "概要", value: "summary" },
  { label: "资产", value: "assets" },
  { label: "注入", value: "modules" },
  { label: "Schema", value: "schema" },
  { label: "转化", value: "conversion" }
]

function buildDimensions(breakdown) {
  return Object.keys(dimensionLabels).map((key) => {
    const value = Number(breakdown && breakdown[key] ? breakdown[key] : 0)
    const max = key === "semantic_clarity" || key === "citation_readiness" ? 20 : 15
    return {
      key,
      label: dimensionLabels[key],
      value,
      max,
      percent: Math.round((value / max) * 100)
    }
  })
}

function buildAssets(result) {
  const title = result.title || "你的页面"
  return [
    {
      name: "AI 摘要定义",
      detail: `为“${title}”写一段 60 字以内、可被 AI 直接引用的业务定义。`
    },
    {
      name: "FAQ 问答库",
      detail: "沉淀 10 个用户会问 AI 的问题，答案保持短句、明确、可验证。"
    },
    {
      name: "对比页面",
      detail: "用表格说明适用场景、差异、优势、限制和替代方案。"
    },
    {
      name: "证据卡片",
      detail: "整理案例、数据、客户反馈和第三方证明，作为 AI 信任信号。"
    }
  ]
}

function buildNextSteps(result) {
  const breakdown = result.breakdown || {}
  const weakDimensions = Object.keys(dimensionLabels)
    .map((key) => {
      const value = Number(breakdown[key] || 0)
      const max = key === "semantic_clarity" || key === "citation_readiness" ? 20 : 15
      return { key, label: dimensionLabels[key], value, max, ratio: value / max }
    })
    .sort((a, b) => a.ratio - b.ratio)
    .slice(0, 2)

  const focus = weakDimensions.map((item) => item.label).join("、") || "FAQ 覆盖、引用友好"

  return [
    {
      key: "advise",
      phase: "第 1 步",
      title: "诊断依据",
      detail: `优先处理 ${focus}，先确认页面为什么不容易被 AI 准确引用。`,
      output: "页面修改清单",
      action: "复制建议"
    },
    {
      key: "produce",
      phase: "第 2 步",
      title: "生成内容包",
      detail: "生成 Hero、AI Summary、Who Should Buy、How to Use、FAQ 和 Schema 草稿。",
      output: "可注入模块文案",
      action: "生成改进"
    },
    {
      key: "export",
      phase: "第 3 步",
      title: "审核发布",
      detail: "保存版本、人工审核、创建发布预览，确认上线后再校验页面。",
      output: "版本 / 发布记录",
      action: "复制内容包"
    },
    {
      key: "index",
      phase: "第 4 步",
      title: "收录推进",
      detail: "把飞书文章或内容包发布到公开 URL，提交 sitemap、站内入口和 llms.txt。",
      output: "公开 URL / 收录清单",
      action: "去文章 Agent"
    },
    {
      key: "sample",
      phase: "第 5 步",
      title: "AI 采样",
      detail: "用目标 Query 到 ChatGPT、豆包、DeepSeek、Perplexity 等平台采样，记录 Mention、Citation 和 Sources。",
      output: "采样记录 / 信源地图",
      action: "生成 Query"
    },
    {
      key: "report",
      phase: "第 6 步",
      title: "复测报告",
      detail: "上线后复测同一 URL，并生成周报沉淀提及率、引用率、分数变化和下一轮动作。",
      output: "复测结果 / 周报",
      action: "生成报告"
    }
  ]
}

function buildStepPanel(result, activeStep) {
  const steps = buildNextSteps(result)
  const step = steps.find((item) => item.key === activeStep) || steps[0]
  const modules = result.injection_modules || []
  const faqs = result.faq_items || []

  if (step.key === "produce") {
    const workflow = result.workflow || defaultWorkflow
    const improved = workflow.improved_modules || []
    const hasDraft = workflow.status === "draft_ready" || improved.length > 0
    return {
      ...step,
      action: hasDraft ? "保存版本进入审核" : "生成改进",
      items: (improved.length ? improved : modules).slice(0, 4).map((item) => `${item.module_type}：${item.title}`),
      note: hasDraft
        ? `已生成改进草稿，预计 GEO 分可提升 ${workflow.score_delta} 分。`
        : (faqs.length ? `已生成 ${faqs.length} 条 FAQ。点击生成改进后，会进入保存版本和人工审核。` : "当前未生成 FAQ，可重新开启 FAQ 选项后分析。")
    }
  }

  if (step.key === "export") {
    const workflow = result.workflow || defaultWorkflow
    const version = result.version || {}
    const retest = result.retest || {}
    const publication = result.selectedPublication || {}
    let action = "保存版本"
    if (version.status === "pending_review") action = "审核通过"
    if (version.status === "approved") {
      action = "创建发布预览"
      if (publication.status === "pending_confirmation") action = "确认发布"
      if (publication.status === "published" || publication.status === "verification_failed") action = "上线校验"
      if (publication.live_status === "verified_live" || publication.status === "verified_live") action = "进入收录推进"
    }
    return {
      ...step,
      action,
      items: workflow.retest_plan && workflow.retest_plan.length
        ? workflow.retest_plan
        : ["复制 JSON 给开发对接", "复制 Markdown 给运营审核", "页面发布后重新诊断同一 URL"],
      note: retest.current_score
        ? `已复测：${retest.previous_score} → ${retest.current_score}，变化 ${retest.score_delta} 分。`
        : (publication.status === "pending_confirmation"
          ? "发布预览已创建。点击主按钮会用 PUBLISH 确认发布，然后进入上线校验。"
          : (publication.status === "published"
            ? "已提交发布。下一步校验线上页面是否包含关键模块。"
            : (publication.live_status === "verified_live" || publication.status === "verified_live"
              ? "上线校验已通过。下一步进入收录推进和 AI 采样。"
              : (version.status === "approved"
                ? "版本已审核通过，下一步创建发布预览。"
          : workflow.injection_payload
            ? "已生成 CMS 字段映射，请先保存版本并审核通过。"
                    : "建议上线后目标提升 10 分以上，弱项至少提升一档。"))))
    }
  }

  if (step.key === "index") {
    return {
      ...step,
      items: ["创建飞书文章草稿", "发布到可抓取公开页面", "填写公开 URL 并提交收录", "记录收录状态后进入 AI 采样"],
      note: "飞书适合协作，不等于公开收录。必须有官网、博客、帮助中心或专题页 URL 才能进入稳定收录。"
    }
  }

  if (step.key === "sample") {
    return {
      ...step,
      items: ["生成或补齐监测 Query", "选择 AI 平台并粘贴回答正文", "粘贴 Sources 链接", "解析后刷新 Mention / Citation"],
      note: "建议每个平台每周至少采 3 条核心 Query，样本不足时只能作为方向参考。"
    }
  }

  if (step.key === "report") {
    return {
      ...step,
      items: ["立即复测同一 URL", "记录 AI 平台采样", "登记线索或转化证据", "生成并确认效果报告"],
      note: "闭环完成的标准不是页面写完，而是能看到分数、收录、Mention、Citation 或线索归因的变化。"
    }
  }

  return {
    ...step,
    items: (result.content_gaps || result.recommendations || []).slice(0, 4),
    note: "先处理前两项弱点，通常比一次性重写整页更容易验收。"
  }
}

function buildBrief(result) {
  const plan = result.growth_plan || []
  const recommendations = result.recommendations || []
  const actions = plan.map((item, index) => `${index + 1}. ${item.title}：${item.action}`).join("\n")
  const recText = recommendations.length ? recommendations.map((item) => `- ${item}`).join("\n") : "- 暂无额外建议"

  return [
    `GEO 推广执行简报`,
    `页面：${result.title || "未命名页面"}`,
    `网址：${result.url || "手动输入内容"}`,
    `当前分数：${result.geo_score}/${result.max_score || 100}`,
    "",
    "本轮目标：",
    "让页面更容易被 AI 搜索、总结、引用、比较和推荐。",
    "",
    "优先动作：",
    actions || "1. 补齐 AI 可引用定义\n2. 生成 FAQ 问答资产\n3. 增加对比和证据",
    "",
    "系统建议：",
    recText,
    "",
    "复测标准：",
    "发布后重新诊断同一网址，目标是总分提升 10 分以上，FAQ、引用友好、权威信号至少各提升一档。"
  ].join("\n")
}

function buildMarkdownExport(result) {
  const modules = (result.injection_modules || [])
    .map((item) => `### ${item.title}\n- 类型：${item.module_type}\n- 位置：${item.target_position}\n- 优先级：${item.priority}\n\n${item.body}`)
    .join("\n\n")
  const faq = (result.faq_items || [])
    .map((item) => `### ${item.question}\n${item.answer}`)
    .join("\n\n")
  const tips = (result.conversion_tips || []).map((item) => `- ${item}`).join("\n")

  return [
    `# GEO 内容注入包`,
    "",
    `URL：${result.url || ""}`,
    `页面：${result.title || ""}`,
    `GEO 分：${result.geo_score || 0}/${result.max_score || 100}`,
    "",
    "## 页面摘要",
    (result.page_summary && result.page_summary.theme) || result.title || "",
    "",
    "## 注入模块",
    modules || "暂无模块",
    "",
    "## FAQ",
    faq || "暂无 FAQ",
    "",
    "## 转化建议",
    tips || "暂无转化建议"
  ].join("\n")
}

function buildExportJson(result) {
  return JSON.stringify({
    task_id: result.task_id,
    url: result.url,
    page_summary: result.page_summary,
    geo_assets: result.geo_assets,
    content_gaps: result.content_gaps,
    injection_modules: result.injection_modules,
    faq_items: result.faq_items,
    schema_suggestions: result.schema_suggestions,
    conversion_tips: result.conversion_tips
  }, null, 2)
}

function buildPublicationState(publications, preferredId) {
  const items = Array.isArray(publications) ? publications : []
  const preferred = preferredId
    ? items.find((item) => item.publication_id === preferredId)
    : (items.find((item) => item.status === "pending_confirmation")
      || items.find((item) => item.status === "failed")
      || items.find((item) => ["published", "verification_failed", "verified_live"].includes(item.status))
      || items[0])
  const selectedPublication = preferred || null
  return {
    selectedPublicationId: selectedPublication ? selectedPublication.publication_id : "",
    selectedPublication,
    selectedPublicationIndex: selectedPublication ? items.findIndex((item) => item.publication_id === selectedPublication.publication_id) : 0
  }
}

function normalizeAnalyzeError(error) {
  const message = (error && error.message) || "诊断失败，请稍后重试"
  if (message.includes("HTTPS/TLS") || message.includes("SSL") || message.includes("抓取失败") || message.includes("Failed to fetch URL")) {
    return `${message}\n\n可切换到“文案分析”，把页面正文粘贴进来继续生成 GEO 文章、FAQ、Schema 和收录推进。`
  }
  if (message.includes("502")) {
    return "后端抓取目标页面失败。请确认页面可公开访问，或切换到“文案分析”粘贴正文继续。"
  }
  return message
}

function buildLoopSteps(data) {
  const monitoring = data.monitoring || {}
  const sampling = monitoring.sampling || {}
  const publication = data.selectedPublication || {}
  return [
    { key: "diagnose", targetStep: "advise", name: "诊断", status: data.result && data.result.task_id ? "done" : "todo", hint: "已得到 GEO 分数和弱项" },
    { key: "produce", targetStep: "produce", name: "生成", status: data.version ? "done" : "current", hint: data.version ? "已有版本草稿" : "保存改进版本" },
    { key: "review", targetStep: "export", name: "审核", status: data.version && data.version.status === "approved" ? "done" : (data.version ? "current" : "todo"), hint: "人工确认可上线内容" },
    { key: "publish", targetStep: "export", name: "发布", status: publication.live_status === "verified_live" ? "done" : (data.selectedPublication ? "current" : "todo"), hint: "创建预览并完成上线校验" },
    { key: "sample", targetStep: "sample", name: "采样", status: sampling.sample_count > 0 ? "done" : "todo", hint: `${sampling.sample_count || 0}/${sampling.sample_target || 0} 样本` },
    { key: "report", targetStep: "report", name: "报告", status: data.reports && data.reports.length ? "done" : "todo", hint: "复测与周报确认" }
  ]
}

Page({
  data: {
    hasAnalyzed: false,
    mode: "url",
    url: "",
    content: "",
    pageTypeOptions,
    languageOptions,
    marketOptions,
    engineOptions,
    resultTabs,
    pageTypeIndex: 0,
    languageIndex: 0,
    marketIndex: 0,
    generateFaq: true,
    generateSchema: true,
    generateConversionTips: true,
    useAi: false,
    clientName: "",
    brandName: "",
    businessGoal: "提升 AI 推荐可见度与询盘",
    selectedEngines: ["chatgpt", "doubao", "deepseek", "perplexity"],
    selectedEngineMap: { chatgpt: true, doubao: true, deepseek: true, perplexity: true },
    activeTab: "summary",
    activeStep: "advise",
    stepPanel: buildStepPanel(defaultResult, "advise"),
    workflow: defaultWorkflow,
    loopSteps: buildLoopSteps({ result: defaultResult, workflow: defaultWorkflow, reports: [] }),
    version: null,
    injection: null,
    retest: null,
    project: null,
    monitoring: null,
    publications: [],
    selectedPublicationId: "",
    selectedPublication: null,
    selectedPublicationIndex: 0,
    feedbackEntries: [],
    experiments: [],
    attributions: [],
    reports: [],
    monitoringQueries: [],
    monitoringQueryIndex: 0,
    monitoringPlatformIndex: 0,
    monitoringPlatforms: aiPlatformOptions,
    aiAnswerText: "",
    aiSourcesText: "",
    sourceParseResult: null,
    sourceParseCompetitorsText: "",
    experimentName: "",
    experimentHypothesis: "",
    experimentVariantA: "",
    experimentVariantB: "",
    attributionSourceType: "chatgpt",
    attributionSourceName: "",
    attributionRevenue: "",
    attributionEvidenceUrl: "",
    reportPeriod: "近 30 天",
    cmsTargets: [],
    cmsTargetIndex: 0,
    publishConfirmation: "",
    verifyTerms: "",
    feedbackVerdict: "accepted",
    feedbackNotes: "",
    improving: false,
    savingVersion: false,
    reviewing: false,
    injecting: false,
    publishing: false,
    verifyingPublication: false,
    savingFeedback: false,
    savingExperiment: false,
    savingAttribution: false,
    generatingReport: false,
    generatingQueries: false,
    parsingSources: false,
    deliveryTarget: "json_file",
    webhookUrl: "",
    retesting: false,
    exporting: false,
    taskList: [],
    loading: false,
    error: "",
    result: defaultResult,
    dimensions: buildDimensions(defaultResult.breakdown),
    assets: buildAssets(defaultResult),
    nextSteps: buildNextSteps(defaultResult)
  },

  onShow() {
    const taskId = wx.getStorageSync("geo_resume_task")
    if (!taskId) {
      return
    }
    wx.removeStorageSync("geo_resume_task")
    this.resumeTask(taskId)
  },

  async resumeTask(taskId) {
    this.setData({ loading: true, error: "" })
    try {
      const detail = await getTaskDetail(taskId)
      const task = detail.task || {}
      const latestVersion = (detail.versions || [])[0] || null
      const latestInjection = (detail.injections || [])[0] || null
      const latestRetest = (detail.retests || [])[0] || task.latest_retest || null
      const baseResult = task.latest_result || defaultResult
      const normalized = {
        ...baseResult,
        is_url_task: true,
        workflow: task.latest_workflow || (latestVersion && latestVersion.workflow) || defaultWorkflow,
        version: latestVersion,
        injection: latestInjection,
        retest: latestRetest,
        project: detail.project || null,
        monitoring: detail.monitoring || null,
        publications: detail.publications || []
      }
      const publicationState = buildPublicationState(detail.publications || [], this.data.selectedPublicationId)
      if (latestVersion && latestVersion.modules && latestVersion.modules.length) {
        normalized.injection_modules = latestVersion.modules
      }
      this.setData({
        hasAnalyzed: true,
        result: normalized,
        workflow: normalized.workflow,
        version: latestVersion,
        injection: latestInjection,
        retest: latestRetest,
        project: detail.project || null,
        monitoring: detail.monitoring || null,
        monitoringQueries: (detail.monitoring && detail.monitoring.queries) || [],
        monitoringQueryIndex: 0,
        publications: detail.publications || [],
        feedbackEntries: detail.feedback || [],
        experiments: detail.experiments || [],
        attributions: detail.attributions || [],
        reports: detail.reports || [],
        ...publicationState,
        dimensions: buildDimensions(normalized.breakdown),
        assets: buildAssets(normalized),
        nextSteps: buildNextSteps(normalized),
        stepPanel: buildStepPanel(normalized, "export"),
        activeStep: "export",
        activeTab: "modules",
        loading: false
      })
      await this.loadCmsTargets()
    } catch (error) {
      this.setData({ loading: false, error: error.message || "恢复任务失败" })
    }
  },

  async loadCmsTargets() {
    try {
      const result = await getCmsTargets()
      this.setData({
        cmsTargets: result.items || [],
        cmsTargetIndex: 0
      })
    } catch (error) {
      this.setData({ error: error.message || "加载 CMS 目标失败" })
    }
  },

  onUrlInput(event) {
    this.setData({ url: event.detail.value, error: "" })
  },

  onContentInput(event) {
    this.setData({ content: event.detail.value, error: "" })
  },

  switchMode(event) {
    const mode = event.currentTarget.dataset.mode
    this.setData({ mode, error: "" })
  },

  changePageType(event) {
    this.setData({ pageTypeIndex: Number(event.detail.value) })
  },

  changeLanguage(event) {
    this.setData({ languageIndex: Number(event.detail.value) })
  },

  changeMarket(event) {
    this.setData({ marketIndex: Number(event.detail.value) })
  },

  toggleOption(event) {
    const key = event.currentTarget.dataset.key
    this.setData({ [key]: !this.data[key] })
  },

  onClientNameInput(event) {
    this.setData({ clientName: event.detail.value })
  },

  onBrandNameInput(event) {
    this.setData({ brandName: event.detail.value })
  },

  onBusinessGoalInput(event) {
    this.setData({ businessGoal: event.detail.value })
  },

  toggleEngine(event) {
    const value = event.currentTarget.dataset.engine
    const selected = this.data.selectedEngines.includes(value)
      ? this.data.selectedEngines.filter((item) => item !== value)
      : [...this.data.selectedEngines, value]
    const selectedEngines = selected.length ? selected : [value]
    const selectedEngineMap = selectedEngines.reduce((map, item) => ({ ...map, [item]: true }), {})
    this.setData({ selectedEngines, selectedEngineMap })
  },

  setDeliveryTarget(event) {
    this.setData({ deliveryTarget: event.currentTarget.dataset.target, error: "" })
  },

  onWebhookInput(event) {
    this.setData({ webhookUrl: event.detail.value, error: "" })
  },

  changeCmsTarget(event) {
    this.setData({ cmsTargetIndex: Number(event.detail.value) })
  },

  onPublishConfirmationInput(event) {
    this.setData({ publishConfirmation: event.detail.value })
  },

  onVerifyTermsInput(event) {
    this.setData({ verifyTerms: event.detail.value })
  },

  onFeedbackNotesInput(event) {
    this.setData({ feedbackNotes: event.detail.value })
  },

  onExperimentNameInput(event) {
    this.setData({ experimentName: event.detail.value })
  },

  onExperimentHypothesisInput(event) {
    this.setData({ experimentHypothesis: event.detail.value })
  },

  onExperimentVariantAInput(event) {
    this.setData({ experimentVariantA: event.detail.value })
  },

  onExperimentVariantBInput(event) {
    this.setData({ experimentVariantB: event.detail.value })
  },

  onAttributionSourceNameInput(event) {
    this.setData({ attributionSourceName: event.detail.value })
  },

  onAttributionRevenueInput(event) {
    this.setData({ attributionRevenue: event.detail.value })
  },

  onAttributionEvidenceUrlInput(event) {
    this.setData({ attributionEvidenceUrl: event.detail.value })
  },

  onReportPeriodInput(event) {
    this.setData({ reportPeriod: event.detail.value })
  },

  onAiAnswerInput(event) {
    this.setData({ aiAnswerText: event.detail.value, error: "" })
  },

  onAiSourcesInput(event) {
    this.setData({ aiSourcesText: event.detail.value, error: "" })
  },

  changeMonitoringQuery(event) {
    this.setData({ monitoringQueryIndex: Number(event.detail.value), sourceParseResult: null })
  },

  changeMonitoringPlatform(event) {
    this.setData({ monitoringPlatformIndex: Number(event.detail.value) })
  },

  setFeedbackVerdict(event) {
    this.setData({ feedbackVerdict: event.currentTarget.dataset.verdict })
  },

  switchTab(event) {
    this.setData({ activeTab: event.currentTarget.dataset.tab })
  },

  switchStep(event) {
    const activeStep = event.currentTarget.dataset.step
    const nextData = {
      activeStep,
      stepPanel: buildStepPanel(this.data.result, activeStep)
    }

    if (activeStep === "advise") {
      nextData.activeTab = "summary"
    }
    if (activeStep === "produce" || activeStep === "export") {
      nextData.activeTab = "modules"
    }

    this.setData(nextData)

    const selectorMap = {
      advise: ".preview-surface",
      produce: ".package-surface",
      export: ".package-surface",
      sample: ".sampling-surface",
      report: ".report-surface"
    }
    const selector = selectorMap[activeStep]
    if (selector) {
      wx.pageScrollTo({ selector, duration: 250 })
    }
  },

  refreshLoopSteps(extra = {}) {
    this.setData({ loopSteps: buildLoopSteps({ ...this.data, ...extra }) })
  },

  async runAudit() {
    const { mode, url, content, pageTypeIndex, languageIndex, marketIndex } = this.data
    const value = mode === "url" ? url.trim() : content.trim()

    if (!value) {
      this.setData({ error: mode === "url" ? "请输入要诊断的网址" : "请输入要诊断的页面内容" })
      return
    }

    this.setData({ loading: true, error: "" })

    try {
      let result
      let taskList = []

      if (mode === "url") {
        const urls = value.split(/\s+/).filter(Boolean).slice(0, 5)
        const payloadBase = {
          page_type: pageTypeOptions[pageTypeIndex].value,
          page_goal: pageTypeOptions[pageTypeIndex].label,
          language: languageOptions[languageIndex].value,
          market: marketOptions[marketIndex].value,
          generate_faq: this.data.generateFaq,
          generate_schema: this.data.generateSchema,
          generate_conversion_tips: this.data.generateConversionTips,
          use_ai: this.data.useAi,
          provider: "openai",
          client_name: this.data.clientName.trim() || null,
          brand_name: this.data.brandName.trim() || null,
          target_engines: this.data.selectedEngines,
          business_goal: this.data.businessGoal.trim()
        }
        const results = await Promise.all(urls.map((item) => analyzeUrl({ ...payloadBase, url: item })))
        result = results[0]
        taskList = results.map((item) => ({
          task_id: item.task_id,
          title: item.title,
          url: item.url,
          score: item.geo_score,
          status: item.status
        }))
      } else {
        result = await auditContent(value)
      }

      const normalized = {
        ...result,
        is_url_task: mode === "url",
        title: result.title || "内容 GEO 诊断",
        url: result.url || "手动输入内容",
        content_preview: result.content_preview || value.slice(0, 600),
        growth_plan: result.growth_plan || defaultResult.growth_plan,
        page_summary: result.page_summary || defaultResult.page_summary,
        geo_assets: result.geo_assets || defaultResult.geo_assets,
        content_gaps: result.content_gaps || result.recommendations || defaultResult.content_gaps,
        injection_modules: result.injection_modules || defaultResult.injection_modules,
        faq_items: result.faq_items || defaultResult.faq_items,
        schema_suggestions: result.schema_suggestions || defaultResult.schema_suggestions,
        conversion_tips: result.conversion_tips || defaultResult.conversion_tips
      }
      normalized.workflow = result.workflow || defaultWorkflow

      this.setData({
        hasAnalyzed: true,
        result: normalized,
        dimensions: buildDimensions(normalized.breakdown),
        assets: buildAssets(normalized),
        nextSteps: buildNextSteps(normalized),
        stepPanel: buildStepPanel(normalized, this.data.activeStep),
        workflow: normalized.workflow,
        loopSteps: buildLoopSteps({
          result: normalized,
          workflow: normalized.workflow,
          version: null,
          selectedPublication: null,
          monitoring: mode === "url" ? {
            sampling: { sample_target: this.data.selectedEngines.length * 9, sample_count: 0 }
          } : null,
          reports: []
        }),
        version: null,
        injection: null,
        retest: null,
        publications: [],
        selectedPublicationId: "",
        selectedPublication: null,
        selectedPublicationIndex: 0,
        feedbackEntries: [],
        experiments: [],
        attributions: [],
        reports: [],
        project: mode === "url" ? {
          client_name: this.data.clientName.trim() || null,
          brand_name: this.data.brandName.trim() || result.title,
          target_engines: this.data.selectedEngines,
          business_goal: this.data.businessGoal.trim(),
          owner: "待分配",
          target_score: 80,
          current_stage: "analyzed",
          next_action: "生成改进内容",
          effectiveness: "尚未复测",
          package_delivery: [],
          gap_actions: [],
          action_progress: { total: 0, done: 0, active: 0, blocked: 0, completion_percent: 0 }
        } : null,
        monitoring: mode === "url" ? {
          active_query_count: this.data.selectedEngines.length * 3,
          queries: [],
          query_count: 0,
          check_count: 0,
          mention_rate: 0,
          citation_rate: 0,
          average_position: null,
          sampling: { sample_target: this.data.selectedEngines.length * 9, sample_count: 0, confidence_level: "none", warning: "尚未录入 AI 平台采样。" },
          source_map: { recommendations: [], domains: [], page_types: [] },
          competitor_gap: [],
          connectors: [],
          connector_status: { count: 0, connected: 0, failed: 0, planned: 0, auditable: 0 }
        } : null,
        monitoringQueries: [],
        monitoringQueryIndex: 0,
        sourceParseResult: null,
        taskList,
        activeTab: "summary",
        loading: false
      })
      if (mode === "url") {
        await this.loadCmsTargets()
        await this.loadMonitoringQueries(true)
      }
    } catch (error) {
      this.setData({
        error: normalizeAnalyzeError(error),
        loading: false
      })
    }
  },

  resetAudit() {
    this.setData({
      hasAnalyzed: false,
      url: "",
      content: "",
      error: "",
      taskList: [],
      activeTab: "summary",
      activeStep: "advise",
      version: null,
      injection: null,
      retest: null,
      publications: [],
      selectedPublicationId: "",
      selectedPublication: null,
      selectedPublicationIndex: 0,
      feedbackEntries: [],
      experiments: [],
      attributions: [],
      reports: [],
      monitoringQueries: [],
      monitoringQueryIndex: 0,
      loopSteps: buildLoopSteps({ result: defaultResult, workflow: defaultWorkflow, reports: [] }),
      aiAnswerText: "",
      aiSourcesText: "",
      sourceParseResult: null,
      sourceParseCompetitorsText: "",
      publishConfirmation: "",
      verifyTerms: "",
      feedbackNotes: "",
      experimentName: "",
      experimentHypothesis: "",
      experimentVariantA: "",
      experimentVariantB: "",
      attributionSourceName: "",
      attributionRevenue: "",
      attributionEvidenceUrl: ""
    })
    wx.pageScrollTo({ scrollTop: 0, duration: 250 })
  },

  copyBrief() {
    const brief = buildBrief(this.data.result)
    wx.setClipboardData({
      data: brief,
      success() {
        wx.showToast({
          title: "已复制简报",
          icon: "success"
        })
      }
    })
  },

  copyJson() {
    const workflow = this.data.result.workflow || {}
    const data = workflow.injection_payload
      ? JSON.stringify(workflow.injection_payload, null, 2)
      : buildExportJson(this.data.result)
    wx.setClipboardData({
      data,
      success() {
        wx.showToast({ title: "JSON 已复制", icon: "success" })
      }
    })
  },

  copyMarkdown() {
    wx.setClipboardData({
      data: buildMarkdownExport(this.data.result),
      success() {
        wx.showToast({ title: "Markdown 已复制", icon: "success" })
      }
    })
  },

  goHistory() {
    wx.navigateTo({ url: "/pages/history/history" })
  },

  runStepAction() {
    const { activeStep } = this.data
    if (activeStep === "produce") {
      const workflow = this.data.result.workflow || {}
      if (workflow.status === "draft_ready" || (workflow.improved_modules || []).length) {
        this.saveDraftVersion()
        return
      }
      this.runImproveWorkflow()
      return
    }

    if (activeStep === "export") {
      const version = this.data.result.version || this.data.version || {}
      if (!version.version_id) {
        this.saveDraftVersion()
        return
      }
      if (version.status !== "approved") {
        this.approveVersion()
        return
      }
      const publication = this.data.selectedPublication
      if (publication && publication.status === "pending_confirmation") {
        this.confirmCmsPublish(true)
        return
      }
      if (publication && ["published", "verification_failed"].includes(publication.status)) {
        this.verifyCmsPublication()
        return
      }
      if (publication && (publication.live_status === "verified_live" || publication.status === "verified_live")) {
        this.setData({
          activeStep: "index",
          stepPanel: buildStepPanel(this.data.result, "index")
        })
        wx.switchTab({ url: "/pages/analysis/analysis" })
        return
      }
      if ((this.data.cmsTargets || []).length && !this.data.selectedPublication) {
        this.createCmsPreview()
        return
      }
      wx.pageScrollTo({ selector: ".package-surface", duration: 250 })
      return
    }

    if (activeStep === "index") {
      wx.switchTab({ url: "/pages/analysis/analysis" })
      return
    }

    if (activeStep === "sample") {
      wx.pageScrollTo({ selector: ".sampling-surface", duration: 250 })
      return
    }

    if (activeStep === "report") {
      wx.pageScrollTo({ selector: ".report-surface", duration: 250 })
      return
    }

    this.copyBrief()
  },

  async runImproveWorkflow() {
    if (this.data.improving) {
      return
    }

    this.setData({ improving: true, error: "" })

    try {
      const workflow = await improvePackage(this.data.result, this.data.useAi)
      const improvedModules = workflow.improved_modules && workflow.improved_modules.length
        ? workflow.improved_modules
        : this.data.result.injection_modules
      const updatedResult = {
        ...this.data.result,
        workflow,
        injection_modules: improvedModules,
        version: null,
        injection: null,
        retest: null
      }

      this.setData({
        result: updatedResult,
        workflow,
        version: null,
        injection: null,
        retest: null,
        stepPanel: buildStepPanel(updatedResult, "produce"),
        loopSteps: buildLoopSteps({ ...this.data, result: updatedResult, workflow, version: null, retest: null }),
        activeTab: "modules",
        improving: false
      })
      wx.pageScrollTo({ selector: ".package-surface", duration: 250 })
      wx.showToast({ title: "已生成，下一步保存版本", icon: "success" })
    } catch (error) {
      this.setData({
        error: error.message || "生成改进草稿失败",
        improving: false
      })
    }
  },

  updateModuleTitle(event) {
    const index = Number(event.currentTarget.dataset.index)
    const modules = (this.data.result.injection_modules || []).map((module, moduleIndex) => {
      if (moduleIndex !== index) {
        return module
      }
      return { ...module, title: event.detail.value, review_status: "edited_pending_save" }
    })
    const result = { ...this.data.result, injection_modules: modules }
    this.setData({
      result,
      stepPanel: buildStepPanel(result, this.data.activeStep)
    })
  },

  updateModuleBody(event) {
    const index = Number(event.currentTarget.dataset.index)
    const modules = (this.data.result.injection_modules || []).map((module, moduleIndex) => {
      if (moduleIndex !== index) {
        return module
      }
      return { ...module, body: event.detail.value, review_status: "edited_pending_save" }
    })
    const result = { ...this.data.result, injection_modules: modules }
    this.setData({
      result,
      stepPanel: buildStepPanel(result, this.data.activeStep)
    })
  },

  async saveDraftVersion() {
    if (!this.data.result.task_id || this.data.result.is_url_task === false) {
      wx.showToast({ title: "内容分析仅支持诊断，请使用 URL 分析进入闭环", icon: "none" })
      return
    }
    if (this.data.savingVersion) {
      return
    }

    this.setData({ savingVersion: true, error: "" })
    try {
      const version = await saveVersion({
        task_id: this.data.result.task_id,
        url: this.data.result.url,
        modules: this.data.result.injection_modules || [],
        workflow: this.data.result.workflow || this.data.workflow
      })
      const result = {
        ...this.data.result,
        version,
        workflow: version.workflow || this.data.result.workflow
      }
      this.setData({
        result,
        version,
        workflow: result.workflow,
        project: {
          ...(this.data.project || {}),
          current_stage: "pending_review",
          next_action: version.quality_report && version.quality_report.status === "blocked" ? "修复内容质量问题" : "人工审核版本"
        },
        stepPanel: buildStepPanel(result, "export"),
        loopSteps: buildLoopSteps({ ...this.data, result, version, workflow: result.workflow }),
        activeStep: "export",
        savingVersion: false
      })
      wx.showToast({ title: "版本已保存，下一步审核", icon: "success" })
    } catch (error) {
      this.setData({
        error: error.message || "保存版本失败",
        savingVersion: false
      })
    }
  },

  async approveVersion() {
    const versionId = this.data.result.version && this.data.result.version.version_id
    if (!versionId) {
      wx.showToast({ title: "请先保存版本", icon: "none" })
      return
    }
    if (this.data.reviewing) {
      return
    }

    this.setData({ reviewing: true, error: "" })
    try {
      const version = await reviewVersion(versionId, "approve")
      const result = {
        ...this.data.result,
        version,
        workflow: version.workflow || this.data.result.workflow
      }
      this.setData({
        result,
        version,
        workflow: result.workflow,
        project: { ...(this.data.project || {}), current_stage: "approved", next_action: "创建发布预览" },
        stepPanel: buildStepPanel(result, "export"),
        loopSteps: buildLoopSteps({ ...this.data, result, version, workflow: result.workflow }),
        activeStep: "export",
        reviewing: false
      })
      wx.showToast({ title: "审核通过", icon: "success" })
    } catch (error) {
      this.setData({
        error: error.message || "审核失败",
        reviewing: false
      })
    }
  },

  async runRetest() {
    const version = this.data.result.version || {}
    if (version.status !== "approved") {
      wx.showToast({ title: "请先审核通过", icon: "none" })
      return
    }
    const injection = this.data.result.injection || this.data.injection
    if (!injection || injection.status !== "completed") {
      wx.showToast({ title: "请先执行交付注入", icon: "none" })
      return
    }
    if (this.data.retesting) {
      return
    }

    this.setData({ retesting: true, error: "" })
    try {
      const retest = await retestTask({
        task_id: this.data.result.task_id,
        url: this.data.result.url,
        previous_score: this.data.result.geo_score,
        approved_payload: version.injection_payload,
        version_id: version.version_id,
        injection_id: injection.injection_id
      })
      const result = {
        ...this.data.result,
        retest
      }
      this.setData({
        result,
        retest,
        stepPanel: buildStepPanel(result, "export"),
        loopSteps: buildLoopSteps({ ...this.data, result, retest }),
        activeStep: "export",
        retesting: false
      })
      wx.showToast({ title: "复测完成", icon: "success" })
    } catch (error) {
      this.setData({
        error: error.message || "复测失败",
        retesting: false
      })
    }
  },

  async scheduleRetest() {
    const version = this.data.result.version || {}
    const injection = this.data.result.injection || this.data.injection
    if (!injection || injection.status !== "completed") {
      wx.showToast({ title: "请先完成发布交付", icon: "none" })
      return
    }
    try {
      await scheduleRetest({
        task_id: this.data.result.task_id,
        url: this.data.result.url,
        previous_score: this.data.result.geo_score,
        version_id: version.version_id,
        injection_id: injection.injection_id,
        max_attempts: 3
      })
      wx.showToast({ title: "已安排复测", icon: "success" })
      this.setData({ project: { ...(this.data.project || {}), next_action: "等待复测结果" } })
    } catch (error) {
      this.setData({ error: error.message || "安排复测失败" })
    }
  },

  async runInjection() {
    const version = this.data.result.version || {}
    if (version.status !== "approved") {
      wx.showToast({ title: "请先审核通过", icon: "none" })
      return
    }
    if (this.data.injecting) {
      return
    }
    if (this.data.deliveryTarget === "webhook" && !this.data.webhookUrl.trim()) {
      wx.showToast({ title: "请输入 CMS Webhook", icon: "none" })
      return
    }
    this.setData({ injecting: true, error: "" })
    try {
      const injection = await injectVersion({
        version_id: version.version_id,
        target: this.data.deliveryTarget,
        webhook_url: this.data.deliveryTarget === "webhook" ? this.data.webhookUrl.trim() : undefined
      })
      const workflowBase = this.data.result.workflow || defaultWorkflow
      const workflow = {
        ...workflowBase,
        stages: (workflowBase.stages || []).map((stage) => {
          if (stage.key === "inject") return { ...stage, status: "completed" }
          if (stage.key === "retest") return { ...stage, status: "ready" }
          return stage
        })
      }
      const result = { ...this.data.result, injection, workflow }
      this.setData({
        result,
        injection,
        workflow,
        project: { ...(this.data.project || {}), current_stage: "injected", next_action: "安排发布后复测" },
        stepPanel: buildStepPanel(result, "export"),
        loopSteps: buildLoopSteps({ ...this.data, result, injection, workflow }),
        activeStep: "export",
        injecting: false
      })
      wx.showToast({ title: "交付已完成", icon: "success" })
    } catch (error) {
      this.setData({ injecting: false, error: error.message || "交付注入失败" })
    }
  },

  async reloadCurrentTask() {
    if (!this.data.result.task_id) {
      return
    }
    const detail = await getTaskDetail(this.data.result.task_id)
    const task = detail.task || {}
    const latestVersion = (detail.versions || [])[0] || this.data.version
    const latestInjection = (detail.injections || [])[0] || this.data.injection
    const latestRetest = (detail.retests || [])[0] || this.data.retest
    const result = {
      ...(task.latest_result || this.data.result),
      workflow: task.latest_workflow || this.data.workflow,
      version: latestVersion,
      injection: latestInjection,
      retest: latestRetest,
      project: detail.project || this.data.project
    }
    if (latestVersion && latestVersion.modules && latestVersion.modules.length) {
      result.injection_modules = latestVersion.modules
    }
    const publicationState = buildPublicationState(detail.publications || [], this.data.selectedPublicationId)
    result.selectedPublication = publicationState.selectedPublication
    this.setData({
      result,
      workflow: result.workflow,
      version: latestVersion,
      injection: latestInjection,
      retest: latestRetest,
      project: detail.project || this.data.project,
      monitoring: detail.monitoring || this.data.monitoring,
      publications: detail.publications || [],
      feedbackEntries: detail.feedback || [],
      experiments: detail.experiments || [],
      attributions: detail.attributions || [],
      reports: detail.reports || [],
      monitoringQueries: (detail.monitoring && detail.monitoring.queries) || this.data.monitoringQueries || [],
      ...publicationState,
      loopSteps: buildLoopSteps({
        ...this.data,
        result,
        workflow: result.workflow,
        version: latestVersion,
        injection: latestInjection,
        retest: latestRetest,
        selectedPublication: publicationState.selectedPublication,
        monitoring: detail.monitoring || this.data.monitoring,
        reports: detail.reports || []
      }),
      stepPanel: buildStepPanel(result, this.data.activeStep)
    })
  },

  async refreshMonitoringSummary() {
    if (!this.data.result.task_id || this.data.result.is_url_task === false) {
      return
    }
    try {
      const monitoring = await getMonitoringSummary(this.data.result.task_id)
      this.setData({
        monitoring,
        monitoringQueries: monitoring.queries || [],
        loopSteps: buildLoopSteps({ ...this.data, monitoring }),
        monitoringQueryIndex: Math.min(this.data.monitoringQueryIndex, Math.max((monitoring.queries || []).length - 1, 0))
      })
    } catch (error) {
      this.setData({ error: error.message || "刷新监测数据失败" })
    }
  },

  async loadMonitoringQueries(autoGenerate = false) {
    if (!this.data.result.task_id || this.data.result.is_url_task === false) {
      return
    }
    try {
      const result = await getMonitoringQueries(this.data.result.task_id)
      const queries = result.items || []
      this.setData({ monitoringQueries: queries, monitoringQueryIndex: 0 })
      if (!queries.length && autoGenerate) {
        await this.generateQueriesForMonitoring()
      } else {
        await this.refreshMonitoringSummary()
      }
    } catch (error) {
      this.setData({ error: error.message || "加载监测 Query 失败" })
    }
  },

  async generateQueriesForMonitoring() {
    if (!this.data.result.task_id || this.data.result.is_url_task === false) {
      wx.showToast({ title: "请先完成 URL 分析", icon: "none" })
      return
    }
    if (this.data.generatingQueries) {
      return
    }
    this.setData({ generatingQueries: true, error: "" })
    try {
      const result = await generateMonitoringQueries({
        task_id: this.data.result.task_id,
        query_count: 12,
        languages: [languageOptions[this.data.languageIndex].value],
        include_competitors: true
      })
      this.setData({
        monitoringQueries: result.items || [],
        monitoringQueryIndex: 0,
        generatingQueries: false
      })
      await this.refreshMonitoringSummary()
      wx.showToast({ title: "Query 已生成", icon: "success" })
    } catch (error) {
      this.setData({ generatingQueries: false, error: error.message || "生成 Query 失败" })
    }
  },

  async parseAiSources() {
    const query = (this.data.monitoringQueries || [])[this.data.monitoringQueryIndex]
    if (!this.data.result.task_id || !query) {
      wx.showToast({ title: "请先生成监测 Query", icon: "none" })
      return
    }
    if (!this.data.aiAnswerText.trim() && !this.data.aiSourcesText.trim()) {
      wx.showToast({ title: "请粘贴 AI 回答或 Sources", icon: "none" })
      return
    }
    this.setData({ parsingSources: true, error: "" })
    try {
      const parsed = await parseMonitoringSources({
        task_id: this.data.result.task_id,
        query_id: query.query_id,
        platform: this.data.monitoringPlatforms[this.data.monitoringPlatformIndex].value,
        answer_text: this.data.aiAnswerText.trim(),
        sources_text: this.data.aiSourcesText.trim(),
        brand_terms: [
          this.data.brandName,
          this.data.project && this.data.project.brand_name,
          this.data.result.title
        ].filter(Boolean),
        competitors: ["Klook", "KKday", "Tripadvisor", "Japan Experience"]
      })
      this.setData({
        sourceParseResult: parsed,
        sourceParseCompetitorsText: ((parsed.parsed && parsed.parsed.competitor_mentions) || []).join("、"),
        parsingSources: false
      })
      await this.refreshMonitoringSummary()
      wx.showToast({ title: "采样已记录", icon: "success" })
    } catch (error) {
      this.setData({ parsingSources: false, error: error.message || "解析 Sources 失败" })
    }
  },

  changeSelectedPublication(event) {
    const selectedPublicationIndex = Number(event.detail.value)
    const publications = this.data.publications || []
    const selectedPublication = publications[selectedPublicationIndex] || null
    const result = { ...this.data.result, selectedPublication }
    this.setData({
      result,
      selectedPublicationIndex,
      selectedPublicationId: selectedPublication ? selectedPublication.publication_id : "",
      selectedPublication,
      stepPanel: buildStepPanel(result, this.data.activeStep),
      loopSteps: buildLoopSteps({ ...this.data, result, selectedPublication })
    })
  },

  async createCmsPreview() {
    const version = this.data.result.version || {}
    const target = this.data.cmsTargets[this.data.cmsTargetIndex]
    if (version.status !== "approved") {
      wx.showToast({ title: "请先审核通过", icon: "none" })
      return
    }
    if (!target) {
      wx.showToast({ title: "请先配置 CMS 目标", icon: "none" })
      return
    }
    this.setData({ publishing: true, error: "" })
    try {
      await createPublicationPreview({ version_id: version.version_id, target_id: target.target_id })
      await this.reloadCurrentTask()
      this.setData({ publishing: false })
      wx.showToast({ title: "预览已创建，下一步确认发布", icon: "success" })
    } catch (error) {
      this.setData({ publishing: false, error: error.message || "创建发布预览失败" })
    }
  },

  async confirmCmsPublish(autoConfirm = false) {
    const publication = this.data.selectedPublication
    if (!publication || publication.status !== "pending_confirmation") {
      wx.showToast({ title: "暂无待确认发布", icon: "none" })
      return
    }
    const confirmation = autoConfirm === true ? "PUBLISH" : this.data.publishConfirmation.trim()
    if (confirmation !== "PUBLISH") {
      wx.showToast({ title: "请输入 PUBLISH", icon: "none" })
      return
    }
    this.setData({ publishing: true, error: "" })
    try {
      await confirmPublication({
        publication_id: publication.publication_id,
        confirmation
      })
      await this.reloadCurrentTask()
      this.setData({ publishing: false, publishConfirmation: "" })
      wx.showToast({ title: "已发布，下一步上线校验", icon: "success" })
    } catch (error) {
      this.setData({ publishing: false, error: error.message || "确认发布失败" })
    }
  },

  async retryCmsPublish() {
    const publication = this.data.selectedPublication || (this.data.publications || []).find((item) => item.status === "failed")
    if (!publication) {
      wx.showToast({ title: "暂无失败发布", icon: "none" })
      return
    }
    this.setData({ publishing: true, error: "" })
    try {
      await retryPublication(publication.publication_id)
      await this.reloadCurrentTask()
      this.setData({ publishing: false })
      wx.showToast({ title: "已重置发布", icon: "success" })
    } catch (error) {
      this.setData({ publishing: false, error: error.message || "重试发布失败" })
    }
  },

  async verifyCmsPublication() {
    const publication = this.data.selectedPublication
    if (!publication) {
      wx.showToast({ title: "暂无可校验发布", icon: "none" })
      return
    }
    if (!["published", "verification_failed", "verified_live"].includes(publication.status)) {
      wx.showToast({ title: "当前发布状态不可校验", icon: "none" })
      return
    }
    const expectedTerms = this.data.verifyTerms
      .split(/\n|,|，/)
      .map((item) => item.trim())
      .filter(Boolean)
    this.setData({ verifyingPublication: true, error: "" })
    try {
      await verifyPublication({
        publication_id: publication.publication_id,
        expected_terms: expectedTerms.length ? expectedTerms : undefined,
        notes: this.data.feedbackNotes.trim() || undefined
      })
      await this.reloadCurrentTask()
      this.setData({ verifyingPublication: false, verifyTerms: "" })
      wx.showToast({ title: "上线校验完成", icon: "success" })
    } catch (error) {
      this.setData({ verifyingPublication: false, error: error.message || "上线校验失败" })
    }
  },

  async scheduleCmsVerification() {
    const publication = this.data.selectedPublication
    if (!publication || !["published", "verification_failed", "verified_live"].includes(publication.status)) {
      wx.showToast({ title: "暂无可安排校验的发布", icon: "none" })
      return
    }
    const expectedTerms = this.data.verifyTerms
      .split(/\n|,|，/)
      .map((item) => item.trim())
      .filter(Boolean)
    try {
      await schedulePublicationVerify({
        publication_id: publication.publication_id,
        expected_terms: expectedTerms.length ? expectedTerms : undefined,
        max_attempts: 5
      })
      await this.reloadCurrentTask()
      wx.showToast({ title: "已安排上线校验", icon: "success" })
    } catch (error) {
      this.setData({ error: error.message || "安排上线校验失败" })
    }
  },

  async submitFeedback() {
    if (!this.data.result.task_id || !this.data.feedbackNotes.trim()) {
      wx.showToast({ title: "请填写反馈说明", icon: "none" })
      return
    }
    const publication = this.data.selectedPublication || {}
    this.setData({ savingFeedback: true, error: "" })
    try {
      await saveFeedback({
        task_id: this.data.result.task_id,
        version_id: this.data.version && this.data.version.version_id,
        publication_id: publication.publication_id,
        verdict: this.data.feedbackVerdict,
        notes: this.data.feedbackNotes.trim(),
        source: "miniapp"
      })
      await this.reloadCurrentTask()
      this.setData({ savingFeedback: false, feedbackNotes: "" })
      wx.showToast({ title: "反馈已记录", icon: "success" })
    } catch (error) {
      this.setData({ savingFeedback: false, error: error.message || "保存反馈失败" })
    }
  },

  async createExperiment() {
    if (!this.data.result.task_id || !this.data.experimentName.trim() || !this.data.experimentHypothesis.trim()) {
      wx.showToast({ title: "请填写实验名称与假设", icon: "none" })
      return
    }
    this.setData({ savingExperiment: true, error: "" })
    try {
      await saveExperiment({
        task_id: this.data.result.task_id,
        name: this.data.experimentName.trim(),
        hypothesis: this.data.experimentHypothesis.trim(),
        channel: "onsite",
        primary_metric: "mention_rate",
        variant_a: this.data.experimentVariantA.trim() || "原始内容结构",
        variant_b: this.data.experimentVariantB.trim() || "改写内容结构",
        status: "draft"
      })
      await this.reloadCurrentTask()
      this.setData({
        savingExperiment: false,
        experimentName: "",
        experimentHypothesis: "",
        experimentVariantA: "",
        experimentVariantB: ""
      })
      wx.showToast({ title: "实验已创建", icon: "success" })
    } catch (error) {
      this.setData({ savingExperiment: false, error: error.message || "创建实验失败" })
    }
  },

  async markExperimentWon(event) {
    const experimentId = event.currentTarget.dataset.experimentId
    if (!experimentId) {
      return
    }
    try {
      await confirmExperiment(experimentId, { status: "won", winner: "variant_b" })
      await this.reloadCurrentTask()
      wx.showToast({ title: "已确认赢家", icon: "success" })
    } catch (error) {
      this.setData({ error: error.message || "确认实验失败" })
    }
  },

  async saveLeadAttribution() {
    if (!this.data.result.task_id || !this.data.attributionSourceName.trim()) {
      wx.showToast({ title: "请填写线索来源", icon: "none" })
      return
    }
    this.setData({ savingAttribution: true, error: "" })
    try {
      await saveAttribution({
        task_id: this.data.result.task_id,
        source_type: this.data.attributionSourceType,
        source_name: this.data.attributionSourceName.trim(),
        attributed_revenue: Number(this.data.attributionRevenue) || 0,
        evidence_url: this.data.attributionEvidenceUrl.trim() || undefined,
        status: "pending_confirmation"
      })
      await this.reloadCurrentTask()
      this.setData({
        savingAttribution: false,
        attributionSourceName: "",
        attributionRevenue: "",
        attributionEvidenceUrl: ""
      })
      wx.showToast({ title: "线索已登记", icon: "success" })
    } catch (error) {
      this.setData({ savingAttribution: false, error: error.message || "保存线索失败" })
    }
  },

  async buildEffectReport() {
    if (!this.data.result.task_id) {
      return
    }
    this.setData({ generatingReport: true, error: "" })
    try {
      await generateReport({
        task_id: this.data.result.task_id,
        period_label: this.data.reportPeriod.trim() || "近 30 天"
      })
      await this.reloadCurrentTask()
      this.setData({ generatingReport: false })
      wx.showToast({ title: "报告已生成", icon: "success" })
    } catch (error) {
      this.setData({ generatingReport: false, error: error.message || "生成报告失败" })
    }
  },

  async confirmLatestReport(event) {
    const reportId = event.currentTarget.dataset.reportId
    if (!reportId) {
      return
    }
    try {
      await confirmReport(reportId, { status: "confirmed" })
      await this.reloadCurrentTask()
      wx.showToast({ title: "报告已确认", icon: "success" })
    } catch (error) {
      this.setData({ error: error.message || "确认报告失败" })
    }
  },

  async exportJsonFile() {
    if (this.data.exporting) {
      return
    }

    const workflow = this.data.result.workflow || {}
    const payload = workflow.injection_payload || this.data.result
    this.setData({ exporting: true, error: "" })

    try {
      const exported = await exportJson({
        task_id: this.data.result.task_id || "manual",
        target: "json_file",
        payload
      })
      wx.setClipboardData({
        data: exported.file_path,
        success() {
          wx.showToast({ title: "导出路径已复制", icon: "success" })
        }
      })
      this.setData({ exporting: false })
    } catch (error) {
      this.setData({
        error: error.message || "导出失败",
        exporting: false
      })
    }
  }
})
