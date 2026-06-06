const {
  getHistory,
  generateMonitoringQueries,
  parseMonitoringSources,
  getMonitoringSummary
} = require("../../utils/api")
const { aiPlatformOptions } = require("../../utils/platforms")

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
    monitoringQueries: [],
    monitoringQueryIndex: 0,
    platformIndex: 0,
    platforms: aiPlatformOptions,
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
      const monitoring = await getMonitoringSummary(taskId)
      this.setData({
        monitoring,
        monitoringQueries: monitoring.queries || [],
        monitoringQueryIndex: 0
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

  onAnswerInput(event) {
    this.setData({ answerText: event.detail.value, error: "" })
  },

  onSourcesInput(event) {
    this.setData({ sourcesText: event.detail.value, error: "" })
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

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.switchTab({ url: "/pages/index/index" })
  }
})
