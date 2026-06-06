const {
  getHistory,
  generateMonitoringQueries,
  parseMonitoringSources,
  getMonitoringSummary,
  getSourceMap,
  getMonitoringConnectors,
  saveMonitoringConnector,
  updateMonitoringConnector,
  getGapActions,
  bootstrapGapActions,
  updateGapAction,
  createGeoArticle
} = require("../../utils/api")
const { aiPlatformOptions } = require("../../utils/platforms")

const connectorTypeOptions = [
  { label: "官方 API", value: "official_api" },
  { label: "官方导出", value: "manual_export" },
  { label: "人工审计", value: "manual_audit" }
]

const connectorStatusOptions = [
  { label: "规划中", value: "planned" },
  { label: "已连通", value: "connected" },
  { label: "失败待修复", value: "failed" }
]

function summarize(history) {
  const checks = history.mention_checks || []
  const mentions = checks.filter((item) => item.brand_mentioned).length
  const citations = checks.filter((item) => item.cited_our_domain).length
  const tasks = history.tasks || []
  const sourceObservations = history.source_observations || []
  return {
    taskCount: tasks.length,
    checkCount: checks.length,
    mentionRate: checks.length ? Math.round((mentions * 100) / checks.length) : 0,
    citationRate: checks.length ? Math.round((citations * 100) / checks.length) : 0,
    sourceCount: sourceObservations.length
  }
}

Page({
  data: {
    loading: false,
    error: "",
    stats: summarize({}),
    tasks: [],
    selectedTaskIndex: 0,
    selectedTask: null,
    monitoring: null,
    sourceMap: null,
    sourceRecommendationCount: 0,
    monitoringQueries: [],
    monitoringQueryIndex: 0,
    platformIndex: 0,
    platforms: aiPlatformOptions,
    connectorTypeOptions,
    connectorStatusOptions,
    connectorTypeIndex: 0,
    connectorStatusIndex: 0,
    connectorProviderName: "",
    connectorCredentialEnv: "",
    connectorEvidenceUrl: "",
    connectors: [],
    bootstrappingActions: false,
    savingConnector: false,
    updatingConnectorId: "",
    gapActions: [],
    articleTitle: "",
    articleFolderToken: "",
    creatingArticle: false,
    articleResult: null,
    answerText: "",
    sourcesText: "",
    parseResult: null,
    parseCompetitorsText: "",
    generatingQueries: false,
    parsingSources: false,
    mentionChecks: [],
    sourceObservations: []
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
      const selectedTask = tasks[this.data.selectedTaskIndex] || tasks[0] || null
      this.setData({
        stats: summarize(history),
        tasks: tasks.slice(0, 20),
        selectedTask,
        mentionChecks: (history.mention_checks || []).slice(0, 8),
        sourceObservations: (history.source_observations || []).slice(0, 8),
        loading: false
      })
      if (selectedTask) {
        await this.loadMonitoring(selectedTask.task_id)
      }
    } catch (error) {
      this.setData({ loading: false, error: error.message || "加载分析数据失败" })
    }
  },

  async loadMonitoring(taskId) {
    try {
      const [monitoring, sourceMapResult, connectorResult, actionResult] = await Promise.all([
        getMonitoringSummary(taskId),
        getSourceMap(taskId),
        getMonitoringConnectors(taskId),
        getGapActions(taskId)
      ])
      this.setData({
        monitoring,
        sourceMap: sourceMapResult,
        sourceRecommendationCount: ((sourceMapResult && sourceMapResult.recommendations) || []).length,
        monitoringQueries: monitoring.queries || [],
        monitoringQueryIndex: 0,
        connectors: connectorResult.items || [],
        gapActions: actionResult.items || []
      })
    } catch (error) {
      this.setData({ error: error.message || "加载监测详情失败" })
    }
  },

  async changeTask(event) {
    const selectedTaskIndex = Number(event.detail.value)
    const selectedTask = this.data.tasks[selectedTaskIndex] || null
    this.setData({
      selectedTaskIndex,
      selectedTask,
      parseResult: null,
      parseCompetitorsText: ""
    })
    if (selectedTask) {
      await this.loadMonitoring(selectedTask.task_id)
    }
  },

  changeQuery(event) {
    this.setData({ monitoringQueryIndex: Number(event.detail.value), parseResult: null })
  },

  changePlatform(event) {
    this.setData({ platformIndex: Number(event.detail.value) })
  },

  changeConnectorType(event) {
    this.setData({ connectorTypeIndex: Number(event.detail.value) })
  },

  changeConnectorStatus(event) {
    this.setData({ connectorStatusIndex: Number(event.detail.value) })
  },

  onConnectorProviderInput(event) {
    this.setData({ connectorProviderName: event.detail.value })
  },

  onConnectorCredentialInput(event) {
    this.setData({ connectorCredentialEnv: event.detail.value })
  },

  onConnectorEvidenceInput(event) {
    this.setData({ connectorEvidenceUrl: event.detail.value })
  },

  onAnswerInput(event) {
    this.setData({ answerText: event.detail.value, error: "" })
  },

  onSourcesInput(event) {
    this.setData({ sourcesText: event.detail.value, error: "" })
  },

  onArticleTitleInput(event) {
    this.setData({ articleTitle: event.detail.value, error: "" })
  },

  onArticleFolderInput(event) {
    this.setData({ articleFolderToken: event.detail.value, error: "" })
  },

  async generateQueries() {
    const task = this.data.selectedTask
    if (!task) {
      wx.showToast({ title: "请先选择任务", icon: "none" })
      return
    }
    this.setData({ generatingQueries: true, error: "" })
    try {
      const result = await generateMonitoringQueries({
        task_id: task.task_id,
        query_count: 12,
        languages: ["zh-HK", "en"],
        include_competitors: true
      })
      this.setData({
        monitoringQueries: result.items || [],
        monitoringQueryIndex: 0,
        generatingQueries: false
      })
      await this.loadMonitoring(task.task_id)
      wx.showToast({ title: "Query 已生成", icon: "success" })
    } catch (error) {
      this.setData({ generatingQueries: false, error: error.message || "生成 Query 失败" })
    }
  },

  async parseSources() {
    const task = this.data.selectedTask
    const query = this.data.monitoringQueries[this.data.monitoringQueryIndex]
    if (!task || !query) {
      wx.showToast({ title: "请先选择任务和 Query", icon: "none" })
      return
    }
    if (!this.data.answerText.trim() && !this.data.sourcesText.trim()) {
      wx.showToast({ title: "请粘贴 AI 回答或 Sources", icon: "none" })
      return
    }
    this.setData({ parsingSources: true, error: "" })
    try {
      const parsed = await parseMonitoringSources({
        task_id: task.task_id,
        query_id: query.query_id,
        platform: this.data.platforms[this.data.platformIndex].value,
        answer_text: this.data.answerText.trim(),
        sources_text: this.data.sourcesText.trim(),
        brand_terms: [task.brand_name, task.title].filter(Boolean),
        competitors: ["Klook", "KKday", "Tripadvisor", "Japan Experience"]
      })
      this.setData({
        parseResult: parsed,
        parseCompetitorsText: ((parsed.parsed && parsed.parsed.competitor_mentions) || []).join("、"),
        parsingSources: false
      })
      await this.loadMonitoring(task.task_id)
      wx.showToast({ title: "采样已记录", icon: "success" })
    } catch (error) {
      this.setData({ parsingSources: false, error: error.message || "解析 Sources 失败" })
    }
  },

  async saveConnector() {
    const task = this.data.selectedTask
    if (!task) {
      wx.showToast({ title: "请先选择任务", icon: "none" })
      return
    }
    if (!this.data.connectorProviderName.trim()) {
      wx.showToast({ title: "请输入接入名称", icon: "none" })
      return
    }
    this.setData({ savingConnector: true, error: "" })
    try {
      await saveMonitoringConnector({
        task_id: task.task_id,
        platform: this.data.platforms[this.data.platformIndex].value,
        connector_type: this.data.connectorTypeOptions[this.data.connectorTypeIndex].value,
        provider_name: this.data.connectorProviderName.trim(),
        status: this.data.connectorStatusOptions[this.data.connectorStatusIndex].value,
        credential_env_var: this.data.connectorCredentialEnv.trim() || null,
        evidence_url: this.data.connectorEvidenceUrl.trim() || null
      })
      await this.loadMonitoring(task.task_id)
      this.setData({
        savingConnector: false,
        connectorProviderName: "",
        connectorCredentialEnv: "",
        connectorEvidenceUrl: ""
      })
      wx.showToast({ title: "接入已保存", icon: "success" })
    } catch (error) {
      this.setData({ savingConnector: false, error: error.message || "保存接入失败" })
    }
  },

  async bootstrapActions() {
    const task = this.data.selectedTask
    if (!task) {
      wx.showToast({ title: "请先选择任务", icon: "none" })
      return
    }
    this.setData({ bootstrappingActions: true, error: "" })
    try {
      await bootstrapGapActions(task.task_id)
      await this.loadMonitoring(task.task_id)
      this.setData({ bootstrappingActions: false })
      wx.showToast({ title: "动作已生成", icon: "success" })
    } catch (error) {
      this.setData({ bootstrappingActions: false, error: error.message || "生成动作失败" })
    }
  },

  async updateConnector(event) {
    const connectorId = event.currentTarget.dataset.connectorId
    const status = event.currentTarget.dataset.status
    if (!connectorId || !status) return
    this.setData({ updatingConnectorId: connectorId, error: "" })
    try {
      await updateMonitoringConnector(connectorId, { status })
      if (this.data.selectedTask) {
        await this.loadMonitoring(this.data.selectedTask.task_id)
      }
      this.setData({ updatingConnectorId: "" })
      wx.showToast({ title: "状态已更新", icon: "success" })
    } catch (error) {
      this.setData({ updatingConnectorId: "", error: error.message || "更新接入失败" })
    }
  },

  async updateAction(event) {
    const actionId = event.currentTarget.dataset.actionId
    const status = event.currentTarget.dataset.status
    if (!actionId || !status) return
    this.setData({ updatingConnectorId: actionId, error: "" })
    try {
      await updateGapAction(actionId, { status })
      if (this.data.selectedTask) {
        await this.loadMonitoring(this.data.selectedTask.task_id)
      }
      this.setData({ updatingConnectorId: "" })
      wx.showToast({ title: "动作已更新", icon: "success" })
    } catch (error) {
      this.setData({ updatingConnectorId: "", error: error.message || "更新动作失败" })
    }
  },

  async createFeishuArticle() {
    const task = this.data.selectedTask
    if (!task) {
      wx.showToast({ title: "请先选择任务", icon: "none" })
      return
    }
    this.setData({ creatingArticle: true, error: "" })
    try {
      const article = await createGeoArticle({
        task_id: task.task_id,
        title: this.data.articleTitle.trim() || `${task.title || "GEO"} 文章优化方案`,
        folder_token: this.data.articleFolderToken.trim() || null,
        use_ai: false,
        publish_to_feishu: true,
        feishu_identity: "bot"
      })
      this.setData({
        creatingArticle: false,
        articleResult: article
      })
      if (article.feishu_url) {
        wx.setClipboardData({
          data: article.feishu_url,
          success() {
            wx.showToast({ title: "飞书链接已复制", icon: "success" })
          }
        })
        return
      }
      wx.setClipboardData({
        data: article.markdown_path || "",
        success() {
          wx.showToast({ title: "本地草稿路径已复制", icon: "success" })
        }
      })
    } catch (error) {
      this.setData({ creatingArticle: false, error: error.message || "创建飞书文章失败" })
    }
  },

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.switchTab({ url: "/pages/index/index" })
  }
})
