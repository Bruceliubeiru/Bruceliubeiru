const { getHistory, reviewVersion } = require("../../utils/api")

Page({
  data: {
    loading: false,
    error: "",
    stats: {
      versions: 0,
      publications: 0,
      feedback: 0,
      blocked: 0
    },
    versions: [],
    publications: [],
    feedbackEntries: [],
    reviewingVersionId: ""
  },

  onShow() {
    this.loadData()
  },

  async loadData() {
    if (this.data.loading) return
    this.setData({ loading: true, error: "" })
    try {
      const history = await getHistory()
      const versions = history.versions || []
      const publications = history.publications || []
      const feedbackEntries = history.feedback_entries || []
      this.setData({
        stats: {
          versions: versions.length,
          publications: publications.length,
          feedback: feedbackEntries.length,
          blocked: versions.filter((item) => item.quality_report && item.quality_report.status === "blocked").length
        },
        versions: versions.slice(0, 6),
        publications: publications.slice(0, 6),
        feedbackEntries: feedbackEntries.slice(0, 6),
        loading: false
      })
    } catch (error) {
      this.setData({ loading: false, error: error.message || "加载优化数据失败" })
    }
  },

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.switchTab({ url: "/pages/index/index" })
  },

  copyVersion(event) {
    const index = Number(event.currentTarget.dataset.index)
    const version = this.data.versions[index]
    if (!version) return
    wx.setClipboardData({
      data: JSON.stringify(version.injection_payload || version, null, 2),
      success() {
        wx.showToast({ title: "已复制", icon: "success" })
      }
    })
  },

  async approveVersion(event) {
    const versionId = event.currentTarget.dataset.versionId
    if (!versionId || this.data.reviewingVersionId) return
    this.setData({ reviewingVersionId: versionId, error: "" })
    try {
      await reviewVersion(versionId, "approve")
      await this.loadData()
      this.setData({ reviewingVersionId: "" })
      wx.showToast({ title: "审核已通过", icon: "success" })
    } catch (error) {
      this.setData({ reviewingVersionId: "", error: error.message || "审核失败" })
    }
  }
})
