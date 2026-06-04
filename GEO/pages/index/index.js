const { auditContent, analyzeUrl, improvePackage, saveVersion, reviewVersion, retestTask } = require("../../utils/api")

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
      title: "给出优化建议",
      detail: `优先处理 ${focus}，先把页面改到能被 AI 准确引用。`,
      output: "页面修改清单",
      action: "复制建议"
    },
    {
      key: "produce",
      phase: "第 2 步",
      title: "生成注入内容",
      detail: "生成 Hero、AI Summary、Who Should Buy、How to Use、FAQ 和 Schema 草稿。",
      output: "可注入模块文案",
      action: "查看注入"
    },
    {
      key: "export",
      phase: "第 3 步",
      title: "导出并复测",
      detail: "把内容包交给运营或开发审核上线，再用同一 URL 复测分数变化。",
      output: "JSON / Markdown / 复测任务",
      action: "复制内容包"
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
    return {
      ...step,
      items: (improved.length ? improved : modules).slice(0, 4).map((item) => `${item.module_type}：${item.title}`),
      note: workflow.status === "draft_ready"
        ? `已生成改进草稿，预计 GEO 分可提升 ${workflow.score_delta} 分。`
        : (faqs.length ? `已生成 ${faqs.length} 条 FAQ，可继续生成改进草稿。` : "当前未生成 FAQ，可重新开启 FAQ 选项后分析。")
    }
  }

  if (step.key === "export") {
    const workflow = result.workflow || defaultWorkflow
    const version = result.version || {}
    const retest = result.retest || {}
    return {
      ...step,
      items: workflow.retest_plan && workflow.retest_plan.length
        ? workflow.retest_plan
        : ["复制 JSON 给开发对接", "复制 Markdown 给运营审核", "页面发布后重新诊断同一 URL"],
      note: retest.current_score
        ? `已复测：${retest.previous_score} → ${retest.current_score}，变化 ${retest.score_delta} 分。`
        : (version.status === "approved"
          ? "版本已审核通过，可以复制注入 JSON 并上线后复测。"
          : workflow.injection_payload
            ? "已生成 CMS 字段映射，请先保存版本并审核通过。"
            : "建议上线后目标提升 10 分以上，弱项至少提升一档。")
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

Page({
  data: {
    mode: "url",
    url: "",
    content: "",
    pageTypeOptions,
    languageOptions,
    marketOptions,
    resultTabs,
    pageTypeIndex: 0,
    languageIndex: 0,
    marketIndex: 0,
    generateFaq: true,
    generateSchema: true,
    generateConversionTips: true,
    activeTab: "summary",
    activeStep: "advise",
    stepPanel: buildStepPanel(defaultResult, "advise"),
    workflow: defaultWorkflow,
    version: null,
    retest: null,
    improving: false,
    savingVersion: false,
    reviewing: false,
    retesting: false,
    taskList: [],
    loading: false,
    error: "",
    result: defaultResult,
    dimensions: buildDimensions(defaultResult.breakdown),
    assets: buildAssets(defaultResult),
    nextSteps: buildNextSteps(defaultResult)
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

  switchTab(event) {
    this.setData({ activeTab: event.currentTarget.dataset.tab })
  },

  switchStep(event) {
    const activeStep = event.currentTarget.dataset.step
    this.setData({
      activeStep,
      stepPanel: buildStepPanel(this.data.result, activeStep)
    })
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
          generate_conversion_tips: this.data.generateConversionTips
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
        result: normalized,
        dimensions: buildDimensions(normalized.breakdown),
        assets: buildAssets(normalized),
        nextSteps: buildNextSteps(normalized),
        stepPanel: buildStepPanel(normalized, this.data.activeStep),
        workflow: normalized.workflow,
        version: null,
        retest: null,
        taskList,
        activeTab: "summary",
        loading: false
      })
    } catch (error) {
      this.setData({
        error: error.message || "诊断失败，请稍后重试",
        loading: false
      })
    }
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

  runStepAction() {
    const { activeStep } = this.data
    if (activeStep === "produce") {
      this.runImproveWorkflow()
      return
    }

    if (activeStep === "export") {
      this.copyJson()
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
      const workflow = await improvePackage(this.data.result)
      const improvedModules = workflow.improved_modules && workflow.improved_modules.length
        ? workflow.improved_modules
        : this.data.result.injection_modules
      const updatedResult = {
        ...this.data.result,
        workflow,
        injection_modules: improvedModules,
        version: null,
        retest: null
      }

      this.setData({
        result: updatedResult,
        workflow,
        version: null,
        retest: null,
        stepPanel: buildStepPanel(updatedResult, "produce"),
        activeTab: "modules",
        improving: false
      })
      wx.pageScrollTo({ selector: ".next-section", duration: 250 })
      wx.showToast({ title: "已生成草稿", icon: "success" })
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
        stepPanel: buildStepPanel(result, this.data.activeStep),
        savingVersion: false
      })
      wx.showToast({ title: "版本已保存", icon: "success" })
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
        stepPanel: buildStepPanel(result, "export"),
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
    if (this.data.retesting) {
      return
    }

    this.setData({ retesting: true, error: "" })
    try {
      const retest = await retestTask({
        task_id: this.data.result.task_id,
        url: this.data.result.url,
        previous_score: this.data.result.geo_score,
        approved_payload: version.injection_payload
      })
      const result = {
        ...this.data.result,
        retest
      }
      this.setData({
        result,
        retest,
        stepPanel: buildStepPanel(result, "export"),
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
  }
})
