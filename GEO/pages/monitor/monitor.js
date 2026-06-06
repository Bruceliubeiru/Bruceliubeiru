const {
  getHistory,
  generateReport,
  confirmReport: confirmReportApi
} = require("../../utils/api")

function buildStats(history) {
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

Page({
  data: {
    loading: false,
    error: "",
    stats: buildStats({}),
    tasks: [],
    selectedTaskIndex: 0,
    selectedTask: null,
    reportPeriod: "近 30 天",
    generatingReport: false,
    confirmingReportId: "",
    experiments: [],
    attributions: [],
    reports: [],
    mentionChecks: []
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
        stats: buildStats(history),
        tasks: tasks.slice(0, 20),
        selectedTask,
        experiments: (history.experiments || []).slice(0, 6),
        attributions: (history.attributions || []).slice(0, 6),
        reports: (history.reports || []).slice(0, 6),
        mentionChecks: (history.mention_checks || []).slice(0, 6),
        loading: false
      })
    } catch (error) {
      this.setData({ loading: false, error: error.message || "加载监测数据失败" })
    }
  },

  changeTask(event) {
    const selectedTaskIndex = Number(event.detail.value)
    this.setData({
      selectedTaskIndex,
      selectedTask: this.data.tasks[selectedTaskIndex] || null
    })
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
      await this.loadData()
      this.setData({ generatingReport: false })
      wx.showToast({ title: "报告已生成", icon: "success" })
    } catch (error) {
      this.setData({ generatingReport: false, error: error.message || "生成报告失败" })
    }
  },

  async confirmReport(event) {
    const reportId = event.currentTarget.dataset.reportId
    if (!reportId) return
    this.setData({ confirmingReportId: reportId, error: "" })
    try {
      await confirmReportApi(reportId, { status: "confirmed" })
      await this.loadData()
      this.setData({ confirmingReportId: "" })
      wx.showToast({ title: "报告已确认", icon: "success" })
    } catch (error) {
      this.setData({ confirmingReportId: "", error: error.message || "确认报告失败" })
    }
  },

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.switchTab({ url: "/pages/index/index" })
  }
})
