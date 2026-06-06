const { getHistory } = require("../../utils/api")

Page({
  data: {
    loading: false,
    error: "",
    stats: {
      tasks: 0,
      versions: 0,
      publications: 0,
      articles: 0
    },
    recentTasks: [],
    recentArticles: []
  },

  onShow() {
    this.loadData()
  },

  async loadData() {
    if (this.data.loading) return
    this.setData({ loading: true, error: "" })
    try {
      const history = await getHistory()
      this.setData({
        stats: {
          tasks: (history.tasks || []).length,
          versions: (history.versions || []).length,
          publications: (history.publications || []).length,
          articles: (history.articles || []).length
        },
        recentTasks: (history.tasks || []).slice(0, 6),
        recentArticles: (history.articles || []).slice(0, 4),
        loading: false
      })
    } catch (error) {
      this.setData({ loading: false, error: error.message || "加载系统数据失败" })
    }
  },

  goHistory() {
    wx.navigateTo({ url: "/pages/history/history" })
  },

  resumeTask(event) {
    const taskId = event.currentTarget.dataset.taskId
    wx.setStorageSync("geo_resume_task", taskId)
    wx.switchTab({ url: "/pages/index/index" })
  },

  copyArticle(event) {
    const index = Number(event.currentTarget.dataset.index)
    const article = this.data.recentArticles[index]
    if (!article) return
    wx.setClipboardData({
      data: article.feishu_url || article.markdown_path || article.article_id,
      success() {
        wx.showToast({ title: "已复制", icon: "success" })
      }
    })
  }
})
