const { getHistory } = require("../../utils/api")

Page({
  data: {
    loading: false,
    error: "",
    tasks: [],
    versions: [],
    displayVersions: [],
    injections: [],
    displayInjections: [],
    publications: [],
    displayPublications: [],
    feedbackEntries: [],
    displayFeedbackEntries: [],
    mentionChecks: [],
    displayMentionChecks: [],
    retestGroups: [],
    displayRetestGroups: [],
    selectedTaskId: "",
    activeTab: "tasks"
  },

  onLoad() {
    this.loadHistory()
  },

  onShow() {
    this.loadHistory()
  },

  switchTab(event) {
    this.setData({ activeTab: event.currentTarget.dataset.tab })
  },

  goBack() {
    wx.navigateBack()
  },

  selectTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    this.setData({
      selectedTaskId: taskId,
      activeTab: "versions",
      displayVersions: this.data.versions.filter((version) => version.task_id === taskId),
      displayInjections: this.data.injections.filter((injection) => injection.task_id === taskId),
      displayPublications: this.data.publications.filter((publication) => publication.task_id === taskId),
      displayRetestGroups: this.data.retestGroups.filter((group) => group.task_id === taskId),
      displayFeedbackEntries: this.data.feedbackEntries.filter((entry) => entry.task_id === taskId),
      displayMentionChecks: this.data.mentionChecks.filter((entry) => entry.task_id === taskId)
    })
  },

  clearFilter() {
    this.setData({
      selectedTaskId: "",
      displayVersions: this.data.versions,
      displayInjections: this.data.injections,
      displayPublications: this.data.publications,
      displayRetestGroups: this.data.retestGroups,
      displayFeedbackEntries: this.data.feedbackEntries,
      displayMentionChecks: this.data.mentionChecks
    })
  },

  async loadHistory() {
    if (this.data.loading) {
      return
    }

    this.setData({ loading: true, error: "" })
    try {
      const history = await getHistory()
      const retests = history.retests || {}
      const retestGroups = Object.keys(retests).map((taskId) => ({
        task_id: taskId,
        items: retests[taskId]
      }))
      const feedbackEntries = history.feedback_entries || []
      const mentionChecks = history.mention_checks || []
      this.setData({
        tasks: history.tasks || [],
        versions: history.versions || [],
        displayVersions: history.versions || [],
        injections: history.injections || [],
        displayInjections: history.injections || [],
        publications: history.publications || [],
        displayPublications: history.publications || [],
        feedbackEntries,
        displayFeedbackEntries: feedbackEntries,
        mentionChecks,
        displayMentionChecks: mentionChecks,
        retestGroups,
        displayRetestGroups: retestGroups,
        loading: false
      })
    } catch (error) {
      this.setData({
        error: error.message || "加载历史失败",
        loading: false
      })
    }
  },

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.navigateBack()
  },

  copyPayload(event) {
    const index = Number(event.currentTarget.dataset.index)
    const version = this.data.displayVersions[index]
    if (!version) {
      return
    }
    wx.setClipboardData({
      data: JSON.stringify(version.injection_payload || version, null, 2),
      success() {
        wx.showToast({ title: "已复制", icon: "success" })
      }
    })
  }
})
