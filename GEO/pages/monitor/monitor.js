const {
  getHistory,
  generateReport,
  confirmReport: confirmReportApi,
  shareReport,
  getTaskDetail,
  updateProject,
  updatePackageDelivery,
  saveMonitoringConnector,
  saveMonitoringConnectorRun,
  updateMonitoringConnector,
  bootstrapGapActions,
  saveGapAction,
  updateGapAction,
  saveExperiment,
  confirmExperiment,
  saveExperimentEvent,
  saveAttribution,
  updateAttribution,
  createGeoArticle,
  updateGeoArticleIndexing,
  getGeoArticleIndexingChecklist,
  parseMonitoringSources,
  saveTrustAnchor,
  updateTrustAnchor,
  exportReportMarkdown,
  exportReportHTML,
  exportReportJSON,
  exportReportDocx,
  exportReportPDF,
  retryPublication,
  verifyPublication,
  schedulePublicationVerify,
  rollbackPublication
} = require("../../utils/api")

const connectorPlatformOptions = ["chatgpt", "perplexity", "gemini", "google_ai_overviews", "claude", "doubao", "deepseek"]
const reportExportOptions = [
  { label: "Markdown", key: "markdown", action: exportReportMarkdown },
  { label: "HTML", key: "html", action: exportReportHTML },
  { label: "JSON", key: "json", action: exportReportJSON },
  { label: "Word(.docx)", key: "docx", action: exportReportDocx },
  { label: "PDF", key: "pdf", action: exportReportPDF }
]

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

function splitKeywords(value = "") {
  return value
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function buildReportPayload(report = {}, monitoring = {}) {
  return {
    task_id: report.task_id,
    report_id: report.report_id,
    summary: {
      mention_rate: report.metrics && report.metrics.mention_rate || 0,
      citation_rate: report.metrics && report.metrics.citation_rate || 0,
      total_checks: report.metrics && report.metrics.check_count || 0,
      platforms_tracked: (monitoring.platform_breakdown || []).length
    },
    findings: report.findings || [],
    recommendations: report.next_actions || [],
    metrics: report.metrics || {}
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
      notes: "",
      last_error: ""
    },
    sourceParse: {
      query_id: "",
      platform: "perplexity",
      answer_text: "",
      sources_text: "",
      competitors: ""
    },
    trustAnchor: {
      channel: "reddit",
      topic: "",
      target_url: "",
      owner: "",
      status: "planned",
      guidance: "",
      evidence_url: ""
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
    experimentEvent: {
      status: "running",
      sample_size: "",
      metric_value: "",
      notes: ""
    },
    attribution: {
      source_type: "ai_platform",
      source_name: "",
      session_ref: "",
      lead_stage: "new",
      attributed_revenue: "",
      evidence_url: "",
      status: "pending_confirmation",
      notes: ""
    },
    action: {
      title: "",
      priority: "P1",
      status: "accepted",
      owner: "",
      notes: "",
      evidence_url: ""
    },
    connectorRun: {
      status: "connected",
      notes: "",
      evidence_url: "",
      last_error: ""
    },
    article: {
      title: "",
      publish_to_feishu: true,
      folder_token: "",
      public_url: "",
      use_ai: false,
      notes: ""
    },
    reportShare: {
      share_channel: "wechat",
      notes: ""
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
    reportExportingId: "",
    lastReportExport: "",
    assigningPackage: false,
    editingConnectorId: "",
    editingActionId: "",
    editingTrustAnchorId: "",
    editingAttributionId: "",
    sourceQueryIndex: 0,
    lastSourceParse: null,
    selectedSourceQueryLabel: "",
    experiments: [],
    experimentEvents: [],
    attributions: [],
    reports: [],
    reportExports: [],
    recentReportExports: [],
    mentionChecks: [],
    servicePackages: [],
    packageIndex: 0,
    monitoring: null,
    gapActions: [],
    publications: [],
    recentPublications: [],
    publicationEvents: [],
    jobs: [],
    recentJobs: [],
    articles: [],
    recentArticleEvents: [],
    articleChecklist: "",
    loadingChecklistFor: "",
    forms: emptyProjectForms(),
    connectorStatusOptions: ["planned", "connected", "failed"],
    connectorTypeOptions: ["official_api", "manual_export", "manual_audit"],
    connectorPlatformOptions,
    verificationMethodOptions: ["human_recorded", "api_response", "export_screenshot", "ops_checklist"],
    sourceParsePlatformOptions: connectorPlatformOptions,
    trustAnchorChannelOptions: ["reddit", "zhihu", "xiaohongshu", "medium", "linkedin", "github", "forum", "media"],
    trustAnchorStatusOptions: ["planned", "in_progress", "done", "rollback"],
    experimentChannelOptions: ["onsite", "cms", "media", "community", "knowledge_base"],
    experimentEventStatusOptions: ["running", "observed", "blocked", "rollback"],
    experimentMetricOptions: ["mention_rate", "citation_rate", "lead_rate", "conversion_rate"],
    attributionSourceOptions: ["ai_platform", "organic_search", "community", "partner", "media"],
    attributionStageOptions: ["new", "qualified", "proposal", "won", "renewal"],
    attributionStatusOptions: ["pending_confirmation", "confirmed", "rejected"],
    articleStatusOptions: ["draft", "published", "indexed", "ai_cited"],
    reportShareChannelOptions: ["wechat", "email", "feishu", "notion", "drive"],
    actionPriorityOptions: ["P0", "P1", "P2"],
    actionStatusOptions: ["accepted", "in_progress", "done", "rollback", "blocked"]
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
      experimentEvents: [],
      attributions: [],
      reports: [],
      reportExports: [],
      recentReportExports: [],
      mentionChecks: [],
      gapActions: [],
      publications: [],
      recentPublications: [],
      publicationEvents: [],
      jobs: [],
      recentJobs: [],
      articles: [],
      recentArticleEvents: [],
      articleChecklist: "",
      loadingChecklistFor: "",
      editingConnectorId: "",
      editingActionId: "",
      editingTrustAnchorId: "",
      editingAttributionId: "",
      sourceQueryIndex: 0,
      lastSourceParse: null,
      selectedSourceQueryLabel: "",
      lastReportExport: "",
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
      loadingChecklistFor: "",
      lastSourceParse: null,
      lastReportExport: ""
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
      const reportExports = detail.report_exports || []
      const publications = detail.publications || []
      const publicationEvents = detail.publication_events || []
      const jobs = detail.jobs || []
      const articles = detail.articles || []
      const experimentEvents = detail.experiment_events || []
      const recentArticleEvents = articles.length && articles[0].index_events
        ? articles[0].index_events.slice(0, 4)
        : []
      const forms = emptyProjectForms()
      const sourceQueries = monitoring && monitoring.queries || []
      const sourceQuery = sourceQueries[0] || null
      forms.article.title = detail.project && detail.project.brand_name
        ? `${detail.project.brand_name} GEO 内容实验稿`
        : ""
      forms.sourceParse.query_id = sourceQuery ? sourceQuery.query_id : ""
      forms.sourceParse.platform = sourceQuery ? sourceQuery.engine : "perplexity"
      forms.trustAnchor.target_url = (detail.task && detail.task.url) || ""
      this.setData({
        selectedProject: detail.project || null,
        monitoring,
        servicePackages,
        packageIndex,
        experiments: detail.experiments || [],
        experimentEvents,
        attributions: detail.attributions || [],
        reports: detail.reports || [],
        reportExports,
        recentReportExports: reportExports.slice(0, 4),
        mentionChecks: (this.data.allMentionChecks || []).filter((item) => item.task_id === taskId).slice(0, 8),
        gapActions: detail.gap_actions || [],
        publications,
        recentPublications: publications.slice(0, 3),
        publicationEvents,
        jobs,
        recentJobs: jobs.slice(0, 2),
        articles,
        recentArticleEvents,
        forms,
        sourceQueryIndex: 0,
        selectedSourceQueryLabel: sourceQuery ? sourceQuery.query_text : "",
        editingConnectorId: "",
        editingActionId: "",
        editingTrustAnchorId: "",
        editingAttributionId: "",
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

  onSourceQueryChange(event) {
    const sourceQueryIndex = Number(event.detail.value)
    const queries = this.data.monitoring && this.data.monitoring.queries || []
    const selected = queries[sourceQueryIndex]
    if (!selected) return
    this.setData({
      sourceQueryIndex,
      selectedSourceQueryLabel: selected.query_text,
      "forms.sourceParse.query_id": selected.query_id,
      "forms.sourceParse.platform": selected.engine
    })
  },

  onPackageChange(event) {
    this.setData({ packageIndex: Number(event.detail.value) })
  },

  applyRecommendedPackage() {
    const project = this.data.selectedProject
    const packages = this.data.servicePackages || []
    const recommended = project && project.recommended_package
    if (!recommended) return
    const packageIndex = packages.findIndex((item) => item.package_id === recommended.package_id)
    if (packageIndex < 0) {
      wx.showToast({ title: "推荐套餐未在当前列表中", icon: "none" })
      return
    }
    this.setData({ packageIndex })
  },

  async assignPackage(event) {
    const task = this.data.selectedTask
    if (!task || this.data.assigningPackage) return
    const packageId = event && event.currentTarget && event.currentTarget.dataset.packageId
    const selectedPackage = packageId
      ? (this.data.servicePackages || []).find((item) => item.package_id === packageId) || null
      : this.data.servicePackages[this.data.packageIndex] || null
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

  async updatePackageDelivery(event) {
    const task = this.data.selectedTask
    const featureKey = event.currentTarget.dataset.featureKey
    const status = event.currentTarget.dataset.status
    if (!task || !featureKey || !status) return
    this.setData({ savingSection: `package-${featureKey}`, error: "" })
    try {
      await updatePackageDelivery(task.task_id, {
        feature_key: featureKey,
        status,
        notes: this.data.forms.action.notes.trim()
      })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "套餐交付已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新套餐交付失败" })
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

  async shareReport(event) {
    const reportId = event.currentTarget.dataset.reportId
    const task = this.data.selectedTask
    const form = this.data.forms.reportShare
    if (!reportId || !task) return
    this.setData({ savingSection: `report-share-${reportId}`, error: "" })
    try {
      await shareReport(reportId, {
        share_channel: form.share_channel,
        notes: form.notes.trim()
      })
      await this.loadProjectDetail(task.task_id)
      this.setData({
        savingSection: "",
        "forms.reportShare.notes": ""
      })
      wx.showToast({ title: "报告已标记分发", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新报告分发失败" })
    }
  },

  async exportReport(event) {
    const reportId = event.currentTarget.dataset.reportId
    const task = this.data.selectedTask
    if (!reportId || !task) return
    const report = (this.data.reports || []).find((item) => item.report_id === reportId)
    if (!report) return
    const projectName = this.data.selectedProject && (this.data.selectedProject.brand_name || this.data.selectedProject.client_name) || task.title || task.task_id
    const payload = buildReportPayload(report, this.data.monitoring || {})
    const labels = reportExportOptions.map((item) => item.label)
    wx.showActionSheet({
      itemList: labels,
      success: async ({ tapIndex }) => {
        const selected = reportExportOptions[tapIndex]
        if (!selected) return
        this.setData({ reportExportingId: reportId, error: "" })
        try {
          const result = await selected.action(projectName, `${projectName} ${report.period_label} GEO 报告`, payload)
          const message = `${selected.key}: ${result.filepath || result.filename || "已导出"}`
          this.setData({ reportExportingId: "", lastReportExport: message })
          wx.setClipboardData({
            data: result.filepath || result.filename || message,
            success() {
              wx.showToast({ title: "导出路径已复制", icon: "success" })
            }
          })
        } catch (error) {
          this.setData({ reportExportingId: "", error: error.message || "导出报告失败" })
        }
      }
    })
  },

  fillConnectorForm(event) {
    const connectorId = event.currentTarget.dataset.connectorId
    const connectors = this.data.monitoring && this.data.monitoring.connectors || []
    const item = connectors.find((entry) => entry.connector_id === connectorId)
    if (!item) return
    this.setData({
      editingConnectorId: connectorId,
      "forms.connector.provider_name": item.provider_name || "",
      "forms.connector.platform": item.platform || "chatgpt",
      "forms.connector.connector_type": item.connector_type || "official_api",
      "forms.connector.status": item.status || "planned",
      "forms.connector.credential_env_var": item.credential_env_var || "",
      "forms.connector.evidence_url": item.evidence_url || "",
      "forms.connector.verification_method": item.verification_method || "human_recorded",
      "forms.connector.notes": item.notes || "",
      "forms.connector.last_error": item.last_error || "",
      "forms.connectorRun.status": item.status || "connected",
      "forms.connectorRun.evidence_url": item.evidence_url || "",
      "forms.connectorRun.last_error": item.last_error || "",
      "forms.connectorRun.notes": ""
    })
  },

  clearConnectorDraft() {
    const forms = this.data.forms
    forms.connector = emptyProjectForms().connector
    forms.connectorRun = emptyProjectForms().connectorRun
    this.setData({ forms, editingConnectorId: "" })
  },

  useConnectorBlueprint(event) {
    const platform = event.currentTarget.dataset.platform
    const blueprints = this.data.monitoring && this.data.monitoring.connector_blueprints || []
    const item = blueprints.find((entry) => entry.platform === platform)
    if (!item) return
    this.clearConnectorDraft()
    this.setData({
      "forms.connector.platform": item.platform,
      "forms.connector.provider_name": item.provider_name || "",
      "forms.connector.connector_type": item.connector_type || "manual_audit",
      "forms.connector.credential_env_var": item.credential_env_var || "",
      "forms.connector.verification_method": item.verification_method || "ops_checklist",
      "forms.connector.notes": item.audit_requirement || ""
    })
  },

  async saveConnector() {
    const task = this.data.selectedTask
    const form = this.data.forms.connector
    if (!task) return
    if (!form.provider_name.trim()) {
      wx.showToast({ title: "请输入接入名称", icon: "none" })
      return
    }
    const isEditing = !!this.data.editingConnectorId
    this.setData({ savingSection: "connector", error: "" })
    try {
      if (isEditing) {
        await updateMonitoringConnector(this.data.editingConnectorId, {
          status: form.status,
          evidence_url: form.evidence_url.trim(),
          last_error: form.last_error.trim(),
          notes: form.notes.trim(),
          verification_method: form.verification_method
        })
      } else {
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
      }
      this.clearConnectorDraft()
      this.setData({ savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: isEditing ? "接入已回填" : "接入已保存", icon: "success" })
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

  async logConnectorRun(event) {
    const task = this.data.selectedTask
    const connectorId = event.currentTarget.dataset.connectorId || this.data.editingConnectorId
    const status = event.currentTarget.dataset.status || this.data.forms.connectorRun.status
    const form = this.data.forms.connectorRun
    if (!task || !connectorId || !status) return
    this.setData({ savingSection: "connector-run", error: "" })
    try {
      await saveMonitoringConnectorRun(connectorId, {
        status,
        notes: form.notes.trim(),
        evidence_url: form.evidence_url.trim(),
        last_error: form.last_error.trim()
      })
      const forms = this.data.forms
      forms.connectorRun = emptyProjectForms().connectorRun
      this.setData({ forms, savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "执行记录已保存", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "保存执行记录失败" })
    }
  },

  async runSourceParse() {
    const task = this.data.selectedTask
    const form = this.data.forms.sourceParse
    if (!task || !form.query_id) {
      wx.showToast({ title: "请先选择 Query", icon: "none" })
      return
    }
    if (!form.answer_text.trim() && !form.sources_text.trim()) {
      wx.showToast({ title: "请粘贴回答或来源", icon: "none" })
      return
    }
    this.setData({ savingSection: "source-parse", error: "" })
    try {
      const result = await parseMonitoringSources({
        task_id: task.task_id,
        query_id: form.query_id,
        platform: form.platform,
        answer_text: form.answer_text.trim(),
        sources_text: form.sources_text.trim(),
        competitors: splitKeywords(form.competitors)
      })
      result.competitor_text = result.check && result.check.competitor_mentions && result.check.competitor_mentions.length
        ? result.check.competitor_mentions.join(" / ")
        : "未识别"
      const forms = this.data.forms
      forms.sourceParse.answer_text = ""
      forms.sourceParse.sources_text = ""
      forms.sourceParse.competitors = ""
      this.setData({ forms, savingSection: "", lastSourceParse: result })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "解析并落库完成", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "解析失败" })
    }
  },

  fillTrustAnchorForm(event) {
    const anchorId = event.currentTarget.dataset.anchorId
    const anchors = this.data.monitoring && this.data.monitoring.trust_anchors || []
    const item = anchors.find((entry) => entry.anchor_id === anchorId)
    if (!item) return
    this.setData({
      editingTrustAnchorId: anchorId,
      "forms.trustAnchor.channel": item.channel || "reddit",
      "forms.trustAnchor.topic": item.topic || "",
      "forms.trustAnchor.target_url": item.target_url || "",
      "forms.trustAnchor.owner": item.owner || "",
      "forms.trustAnchor.status": item.status || "planned",
      "forms.trustAnchor.guidance": item.guidance || "",
      "forms.trustAnchor.evidence_url": item.evidence_url || ""
    })
  },

  clearTrustAnchorDraft() {
    const forms = this.data.forms
    forms.trustAnchor = emptyProjectForms().trustAnchor
    forms.trustAnchor.target_url = this.data.selectedTask && this.data.selectedTask.url || ""
    this.setData({ forms, editingTrustAnchorId: "" })
  },

  async saveTrustAnchorDraft() {
    const task = this.data.selectedTask
    const form = this.data.forms.trustAnchor
    if (!task) return
    if (!form.topic.trim()) {
      wx.showToast({ title: "请输入锚点主题", icon: "none" })
      return
    }
    this.setData({ savingSection: "trust-anchor", error: "" })
    try {
      if (this.data.editingTrustAnchorId) {
        await updateTrustAnchor(this.data.editingTrustAnchorId, {
          channel: form.channel,
          topic: form.topic.trim(),
          status: form.status,
          owner: form.owner.trim(),
          target_url: form.target_url.trim(),
          guidance: form.guidance.trim(),
          evidence_url: form.evidence_url.trim()
        })
      } else {
        await saveTrustAnchor({
          task_id: task.task_id,
          channel: form.channel,
          topic: form.topic.trim(),
          target_url: form.target_url.trim(),
          owner: form.owner.trim(),
          status: form.status,
          guidance: form.guidance.trim(),
          evidence_url: form.evidence_url.trim()
        })
      }
      this.clearTrustAnchorDraft()
      this.setData({ savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: "Trust Anchor 已保存", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "保存 Trust Anchor 失败" })
    }
  },

  async updateTrustAnchorStatus(event) {
    const task = this.data.selectedTask
    const anchorId = event.currentTarget.dataset.anchorId
    const status = event.currentTarget.dataset.status
    if (!task || !anchorId || !status) return
    this.setData({ savingSection: `anchor-${anchorId}`, error: "" })
    try {
      await updateTrustAnchor(anchorId, { status })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "Trust Anchor 已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新 Trust Anchor 失败" })
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

  fillGapActionForm(event) {
    const actionId = event.currentTarget.dataset.actionId
    const item = (this.data.gapActions || []).find((entry) => entry.action_id === actionId)
    if (!item) return
    this.setData({
      "forms.action.title": item.title || "",
      "forms.action.priority": item.priority || "P1",
      "forms.action.status": item.status || "accepted",
      "forms.action.owner": item.owner || "",
      "forms.action.notes": item.notes || "",
      "forms.action.evidence_url": item.evidence_url || "",
      editingActionId: actionId
    })
  },

  clearGapActionDraft() {
    const forms = this.data.forms
    forms.action = emptyProjectForms().action
    this.setData({ forms, editingActionId: "" })
  },

  async saveGapActionDraft() {
    const task = this.data.selectedTask
    const form = this.data.forms.action
    if (!task) return
    if (!form.title.trim()) {
      wx.showToast({ title: "请输入行动项标题", icon: "none" })
      return
    }
    const isEditing = !!this.data.editingActionId
    this.setData({ savingSection: "action-draft", error: "" })
    try {
      if (isEditing) {
        await updateGapAction(this.data.editingActionId, {
          title: form.title.trim(),
          priority: form.priority,
          status: form.status,
          owner: form.owner.trim(),
          notes: form.notes.trim(),
          evidence_url: form.evidence_url.trim()
        })
      } else {
        await saveGapAction({
          task_id: task.task_id,
          title: form.title.trim(),
          action_type: "manual_followup",
          source: "manual",
          priority: form.priority,
          status: form.status,
          owner: form.owner.trim(),
          notes: form.notes.trim(),
          evidence_url: form.evidence_url.trim()
        })
      }
      this.clearGapActionDraft()
      this.setData({ savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: isEditing ? "行动项已更新" : "行动项已创建", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "保存行动项失败" })
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

  async saveExperimentEvent(event) {
    const task = this.data.selectedTask
    const experimentId = event.currentTarget.dataset.experimentId
    const status = event.currentTarget.dataset.status || this.data.forms.experimentEvent.status
    const form = this.data.forms.experimentEvent
    if (!task || !experimentId || !status) return
    this.setData({ savingSection: `experiment-event-${experimentId}`, error: "" })
    try {
      await saveExperimentEvent(experimentId, {
        status,
        sample_size: form.sample_size ? Number(form.sample_size) : undefined,
        metric_value: form.metric_value ? Number(form.metric_value) : undefined,
        notes: form.notes.trim()
      })
      await this.loadProjectDetail(task.task_id)
      this.setData({
        savingSection: "",
        "forms.experimentEvent.sample_size": "",
        "forms.experimentEvent.metric_value": "",
        "forms.experimentEvent.notes": ""
      })
      wx.showToast({ title: "实验记录已保存", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "保存实验记录失败" })
    }
  },

  fillAttributionForm(event) {
    const attributionId = event.currentTarget.dataset.attributionId
    const item = (this.data.attributions || []).find((entry) => entry.attribution_id === attributionId)
    if (!item) return
    this.setData({
      editingAttributionId: attributionId,
      "forms.attribution.source_type": item.source_type || "ai_platform",
      "forms.attribution.source_name": item.source_name || "",
      "forms.attribution.session_ref": item.session_ref || "",
      "forms.attribution.lead_stage": item.lead_stage || "new",
      "forms.attribution.attributed_revenue": `${item.attributed_revenue || ""}`,
      "forms.attribution.evidence_url": item.evidence_url || "",
      "forms.attribution.status": item.status || "pending_confirmation",
      "forms.attribution.notes": item.notes || ""
    })
  },

  clearAttributionDraft() {
    const forms = this.data.forms
    forms.attribution = emptyProjectForms().attribution
    this.setData({ forms, editingAttributionId: "" })
  },

  async saveAttributionDraft() {
    const task = this.data.selectedTask
    const form = this.data.forms.attribution
    if (!task) return
    if (!form.source_name.trim()) {
      wx.showToast({ title: "请输入线索来源", icon: "none" })
      return
    }
    const isEditing = !!this.data.editingAttributionId
    this.setData({ savingSection: "attribution", error: "" })
    try {
      if (isEditing) {
        await updateAttribution(this.data.editingAttributionId, {
          source_name: form.source_name.trim(),
          session_ref: form.session_ref.trim(),
          lead_stage: form.lead_stage,
          attributed_revenue: Number(form.attributed_revenue || 0),
          evidence_url: form.evidence_url.trim(),
          status: form.status,
          notes: form.notes.trim()
        })
      } else {
        await saveAttribution({
          task_id: task.task_id,
          source_type: form.source_type,
          source_name: form.source_name.trim(),
          session_ref: form.session_ref.trim(),
          lead_stage: form.lead_stage,
          attributed_revenue: Number(form.attributed_revenue || 0),
          evidence_url: form.evidence_url.trim(),
          status: form.status,
          notes: form.notes.trim()
        })
      }
      this.clearAttributionDraft()
      this.setData({ savingSection: "" })
      await this.loadProjectDetail(task.task_id)
      wx.showToast({ title: isEditing ? "归因已更新" : "归因已登记", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "登记归因失败" })
    }
  },

  async quickUpdateAttribution(event) {
    const task = this.data.selectedTask
    const attributionId = event.currentTarget.dataset.attributionId
    const status = event.currentTarget.dataset.status
    if (!task || !attributionId || !status) return
    this.setData({ savingSection: `attribution-${attributionId}`, error: "" })
    try {
      await updateAttribution(attributionId, { status })
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "归因状态已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新归因失败" })
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

  async quickPublicationAction(event) {
    const task = this.data.selectedTask
    const publicationId = event.currentTarget.dataset.publicationId
    const action = event.currentTarget.dataset.action
    if (!task || !publicationId || !action) return
    this.setData({ savingSection: `publication-${publicationId}-${action}`, error: "" })
    try {
      if (action === "retry") {
        await retryPublication(publicationId)
      } else if (action === "verify") {
        await verifyPublication({ publication_id: publicationId })
      } else if (action === "schedule_verify") {
        await schedulePublicationVerify({ publication_id: publicationId, max_attempts: 5 })
      } else if (action === "rollback") {
        await rollbackPublication({
          publication_id: publicationId,
          status: "rollback_completed",
          notes: this.data.forms.article.notes.trim() || "人工确认已回滚。"
        })
      }
      await this.loadProjectDetail(task.task_id)
      this.setData({ savingSection: "" })
      wx.showToast({ title: "发布状态已更新", icon: "success" })
    } catch (error) {
      this.setData({ savingSection: "", error: error.message || "更新发布状态失败" })
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
        public_url: publicUrl,
        notes: this.data.forms.article.notes.trim()
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
