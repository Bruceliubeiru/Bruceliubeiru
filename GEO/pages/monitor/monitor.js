const {
  getHistory,
  generateReport,
  confirmReport: confirmReportApi,
  getTaskDetail,
  updateProject,
  saveMonitoringConnector,
  updateMonitoringConnector,
  bootstrapGapActions,
  updateGapAction,
  saveExperiment,
  confirmExperiment,
  saveAttribution,
  createGeoArticle,
  updateGeoArticleIndexing,
  getGeoArticleIndexingChecklist
} = require("../../utils/api")

function buildStats(history = {}) {
  const checks = history.mention_checks || []
  const mentions = checks.filter((item) => item.brand_mentioned).length
  const citations = checks.filter((item) => item.cited_our_domain).length
  const attributions = history.attributions || []
  const revenue = attributions.reduce((sum, item) => sum + Number(item.attributed_revenue || 0), 0)

  return {
    mentionRate: checks.length ? Math.round((mentions * 100) / checks.length) : 0,
    citationRate: checks.length ? Math.round((citations * 100) / checks.length) : 0,
    experiments: (history.experiments || []).length,
    revenue
  }
}

function emptyProjectForms() {
  return {
    connector: {
      provider_name: "",
      platform: "chatgpt",
      connector_type: "official_api",
      status: "planned",
      credential_env_var: "",
      evidence_url: "",
      verification_method: "human_recorded",
      notes: ""
    },
    experiment: {
      name: "",
      hypothesis: "",
      channel: "onsite",
      primary_metric: "mention_rate",
      variant_a: "",
      variant_b: "",
      notes: ""
    },
    attribution: {
      source_type: "ai_platform",
      source_name: "",
      lead_stage: "new",
      attributed_revenue: "",
      evidence_url: "",
      status: "pending_confirmation",
      notes: ""
    },
    article: {
      title: "",
      publish_to_feishu: true,
      folder_token: "",
      public_url: "",
      use_ai: false
    }
  }
}

Page({
  data: {
    loading: false,
    detailLoading: false,
    error: "",
    savingSection: "",
    stats: buildStats({}),
    tasks: [],
    allMentionChecks: [],
    selectedTaskIndex: 0,
    selectedTask: null,
    selectedProject: null,
    reportPeriod: "近 30 天",
    generatingReport: false,
    confirmingReportId: "",
    assigningPackage: false,
    experiments: [],
    attributions: [],
    reports: [],
    mentionChecks: [],
    servicePackages: [],
    packageIndex: 0,
    monitoring: null,
    gapActions: [],
    articles: [],
    articleChecklist: "",
    loadingChecklistFor: "",
    forms: emptyProjectForms(),
    connectorStatusOptions: ["planned", "connected", "failed"],
    connectorTypeOptions: ["official_api", "manual_export", "manual_audit"],
    connectorPlatformOptions: ["chatgpt", "perplexity", "gemini", "google_ai_overviews", "claude", "doubao", "deepseek"],
    verificationMethodOptions: ["human_recorded", "api_response", "export_screenshot", "ops_checklist"],
    experimentChannelOptions: ["onsite", "cms", "media", "community", "knowledge_base"],
    experimentMetricOptions: ["mention_rate", "citation_rate", "lead_rate", "conversion_rate"],
    attributionSourceOptions: ["ai_platform", "organic_search", "community", "partner", "media"],
    attributionStageOptions: ["new", "qualified", "proposal", "won", "renewal"],
    attributionStatusOptions: ["pending_confirmation", "confirmed", "rejected"],
    articleStatusOptions: ["draft", "published", "indexed", "ai_cited"]
  },

  onShow() {
    this.loadData()
  },

  async loadData() {
    if (this.data.loading) return
    this.setData({ loading: true, error: "" })
    try {
      const history = await getHistory()
      const tasks = history.tasks || []
      const allMentionChecks = history.mention_checks || []
      let selectedTaskIndex = this.data.selectedTaskIndex
      if (selectedTaskIndex >= tasks.length) selectedTaskIndex = 0
      const selectedTask = tasks[selectedTaskIndex] || tasks[0] || null
      this.setData({
        stats: buildStats(history),
        tasks: tasks.slice(0, 30),
        allMentionChecks,
        selectedTaskIndex,
        selectedTask,
        loading: false
      })
      if (selectedTask) {
        await this.loadProjectDetail(selectedTask.task_id)
      } else {
        this.resetTaskDetail()
      }
    } catch (error) {
      this.setData({ loading: false, error: error.message || "加载监测数据失败" })
    }
  },

  resetTaskDetail() {
    this.setData({
      selectedProject: null,
      monitoring: null,
      servicePackages: [],
      packageIndex: 0,
      experiments: [],
      attributions: [],
      reports: [],
      mentionChecks: [],
      gapActions: [],
      articles: [],
      articleChecklist: "",
      loadingChecklistFor: "",
      forms: emptyProjectForms()
    })
  },

  changeTask(event) {
    const selectedTaskIndex = Number(event.detail.value)
    const selectedTask = this.data.tasks[selectedTaskIndex] || null
    this.setData({
      selectedTaskIndex,
      selectedTask,
      articleChecklist: "",
      loadingChecklistFor: ""
    })
    if (selectedTask) {
      this.loadProjectDetail(selectedTask.task_id)
    } else {
      this.resetTaskDetail()
    }
  },

  async loadProjectDetail(taskId) {
    if (!taskId) return
    this.setData({ detailLoading: true, error: "" })
    try {
      const detail = await getTaskDetail(taskId)
      const servicePackages = detail.service_packages || []
      const packageId = detail.project && detail.project.package_id
      const packageIndex = Math.max(servicePackages.findIndex((item) => item.package_id === packageId), 0)
      const monitoring = detail.monitoring || null
      const forms = emptyProjectForms()
      forms.article.title = detail.project && detail.project.brand_name
        ? `${detail.project.brand_name} GEO 内容实验稿`
        : ""
      this.setData({
        selectedProject: detail.project || null,
        monitoring,
        servicePackages,
        packageIndex,
        experiments: detail.experiments || [],
        attributions: detail.attributions || [],
        reports: detail.reports || [],
        mentionChecks: (this.data.allMentionChecks || []).filter((item) => item.task_id === taskId).slice(0, 8),
        gapActions: detail.gap_actions || [],
        articles: detail.articles || [],
        forms,
        detailLoading: false
      })
    } catch (error) {
      this.setData({ detailLoading: false, error: error.message || "加载项目详情失败" })
    }
  },

  onFieldInput(event) {
    const path = event.currentTarget.dataset.path
    if (!path) return
    this.setData({ [path]: event.detail.value })
  },

  onSwitchChange(event) {
    const path = event.currentTarget.dataset.path
    if (!path) return
    this.setData({ [path]: event.detail.value })
  },

  onPickerChange(event) {
    const path = event.currentTarget.dataset.path
    const optionsName = event.currentTarget.dataset.options
    const options = this.data[optionsName] || []
    const selected = options[Number(event.detail.value)]
    if (!path || selected === undefined) return
    this.setData({ [path]: selected })
  },

  onPackageChange(event) {
    this.setData({ packageIndex: Number(event.detail.value) })
  },

  async assignPackage() {
    const task = this.data.selectedTask
    if (!task || this.data.assigningPackage) return
    const selectedPackage = this.data.servicePackages[this.data.packageIndex] || null
    this.setData({ assigningPackage: true, error: "" })
    try {
      await updateProject(task.task_id, {
        package_id: selectedPackage ? selectedPackage.package_id : null,
        service_tier: selectedPackage ? selectedPackage.tier : null
      })
      await this.loadProjectDetail(task.task_id)
      this.setData({ assigningPackage: false })
      wx.showToast({ title: "套餐已绑定", icon: "success" })
    } catch (error) {
      this.setData({ assigningPackage: false, error: error.message || "绑定套餐失败" })
    }
  },

  onReportPeriodInput(event) {
    this.setData({ reportPeriod: event.detail.value })
  },

  async buildReport() {
    const task = this.data.selectedTask
    if (!task) {
      wx.showToast({ title: "请先选择任务", icon: "none" })
      return
    }
    this.setData({ generatingReport: true, error: "" })
    try {
      await generateReport({
        task_id: task.task_id,
        period_label: this.data.reportPeriod.trim() || "近 30 天"
      })
      await this.loadProjectDetail(task.task_id)
      this.setData({ generatingReport: false })
      wx.showToast({ title: "报告已生成", icon: "success" })
    } catch (error) {
      this.setData({ generatingReport: false, error: error.message || "生成报告失败" })
    }
  },

  async confirmReport(event) {
    const reportId = event.currentTarget.dataset.reportId
    const task = this.data.selectedTask
    if (!reportId || !task) return
    this.setData({ confirmingReportId: reportId, error: "" })
    try {
      await confirmReportApi(reportId, { status: "confirmed" })
      await this.loadProjectDetail(task.task_id)
      this.setData({ confirmingReportId: "" })
      wx.showToast({ title: "报告已确认", icon: "success" })
    } catch (error) {
      this.setData({ confirmingReportId: "", error: error.message || "确认报告失败" })
    }
  },

  async saveConnector() {
    const task = this.data.selectedTask
    const form = this.data.forms.connector
    if (!task) return
    if (!form.provider_name.trim()) {
      wx.showToast({ title: "请输入接入名称", icon: "none" })
      return
    }
    this.setData({ savingSection: "connector", error: "" })
    try {
      await saveMonitoringConnector({
        task_id: task.task_id,
        platform: form.platform,
        connector_type: form.connector_type,
        provider_name: form.provider_name.trim(),
        status: form.status,
        credential_env_var: form.credential_env_var.trim(),
        evidence_url: form.evidence_url.trim(),
        verification_method: form.verification_method,
        notes: form.notes.trim()
      })
      const forms = this.data.forms
      forms.connector = emptyProjectForms().connector
      this.setData({ forms, savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "接入已保存", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "保存接入失败" })
    }
  },

  async markConnectorStatus(event) {
    const task = this.data.selectedTask
    const connectorId = event.currentTarget.dataset.connectorId
    const status = event.currentTarget.dataset.status
    if (!task || !connectorId || !status) return
    this.setData({ savingSection: `connector-${connectorId}`, error: "" })
    try {
      await updateMonitoringConnector(connectorId, { status })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "接入状态已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新接入失败" })
    }
  },

  async bootstrapActions() {
    const task = this.data.selectedTask
    if (!task) return
    this.setData({ savingSection: "actions", error: "" })
    try {
      await bootstrapGapActions(task.task_id)
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "行动项已生成", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "生成行动项失败" })
    }
  },

  async markGapAction(event) {
    const task = this.data.selectedTask
    const actionId = event.currentTarget.dataset.actionId
    const status = event.currentTarget.dataset.status
    if (!task || !actionId || !status) return
    this.setData({ savingSection: `action-${actionId}`, error: "" })
    try {
      await updateGapAction(actionId, { status })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "行动项已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新行动项失败" })
    }
  },

  async createExperiment() {
    const task = this.data.selectedTask
    const form = this.data.forms.experiment
    if (!task) return
    if (!form.name.trim() || !form.hypothesis.trim() || !form.variant_a.trim() || !form.variant_b.trim()) {
      wx.showToast({ title: "请补全实验字段", icon: "none" })
      return
    }
    this.setData({ savingSection: "experiment", error: "" })
    try {
      await saveExperiment({
        task_id: task.task_id,
        name: form.name.trim(),
        hypothesis: form.hypothesis.trim(),
        channel: form.channel,
        primary_metric: form.primary_metric,
        variant_a: form.variant_a.trim(),
        variant_b: form.variant_b.trim(),
        notes: form.notes.trim()
      })
      const forms = this.data.forms
      forms.experiment = emptyProjectForms().experiment
      this.setData({ forms, savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "实验已创建", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "创建实验失败" })
    }
  },

  async confirmExperimentResult(event) {
    const task = this.data.selectedTask
    const experimentId = event.currentTarget.dataset.experimentId
    const winner = event.currentTarget.dataset.winner
    if (!task || !experimentId || !winner) return
    this.setData({ savingSection: `experiment-${experimentId}`, error: "" })
    try {
      await confirmExperiment(experimentId, { status: "won", winner })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "实验结果已确认", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "确认实验失败" })
    }
  },

  async createAttribution() {
    const task = this.data.selectedTask
    const form = this.data.forms.attribution
    if (!task) return
    if (!form.source_name.trim()) {
      wx.showToast({ title: "请输入线索来源", icon: "none" })
      return
    }
    this.setData({ savingSection: "attribution", error: "" })
    try {
      await saveAttribution({
        task_id: task.task_id,
        source_type: form.source_type,
        source_name: form.source_name.trim(),
        lead_stage: form.lead_stage,
        attributed_revenue: Number(form.attributed_revenue || 0),
        evidence_url: form.evidence_url.trim(),
        status: form.status,
        notes: form.notes.trim()
      })
      const forms = this.data.forms
      forms.attribution = emptyProjectForms().attribution
      this.setData({ forms, savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "归因已登记", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "登记归因失败" })
    }
  },

  async createArticleDraft() {
    const task = this.data.selectedTask
    const form = this.data.forms.article
    if (!task) return
    this.setData({ savingSection: "article", error: "" })
    try {
      await createGeoArticle({
        task_id: task.task_id,
        title: form.title.trim(),
        folder_token: form.folder_token.trim(),
        use_ai: !!form.use_ai,
        publish_to_feishu: !!form.publish_to_feishu
      })
      const forms = this.data.forms
      forms.article = emptyProjectForms().article
      this.setData({ forms, savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "文章草稿已生成", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "创建文章失败" })
    }
  },

  async updateArticleStatus(event) {
    const task = this.data.selectedTask
    const articleId = event.currentTarget.dataset.articleId
    const status = event.currentTarget.dataset.status
    const publicUrl = event.currentTarget.dataset.publicUrl || this.data.forms.article.public_url || ""
    if (!task || !articleId || !status) return
    this.setData({ savingSection: `article-${articleId}`, error: "" })
    try {
      await updateGeoArticleIndexing(articleId, {
        index_status: status,
        public_url: publicUrl
      })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "文章状态已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新文章状态失败" })
    }
  },

  async copyChecklist(event) {
    const articleId = event.currentTarget.dataset.articleId
    if (!articleId) return
    this.setData({ loadingChecklistFor: articleId, error: "" })
    try {
      const result = await getGeoArticleIndexingChecklist(articleId)
      const markdown = result.markdown || ""
      this.setData({ articleChecklist: markdown, loadingChecklistFor: "" })
      wx.setClipboardData({
        data: markdown,
        success() {
          wx.showToast({ title: "清单已复制", icon: "success" })
        }
      })
    } catch (error) {
      this.setData({ loadingChecklistFor: "", error: error.message || "加载清单失败" })
    }
  },

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.switchTab({ url: "/pages/index/index" })
  }
})
