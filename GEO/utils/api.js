const app = getApp()

function request(path, data, method = "POST") {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${app.globalData.apiBase}${path}`,
      method,
      data,
      header: {
        "Content-Type": "application/json"
      },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
          return
        }

        const detail = res.data && res.data.detail
        reject(new Error(detail || `请求失败：${res.statusCode}`))
      },
      fail(error) {
        const message = error.errMsg || "无法连接 GEO 后端"
        if (message.includes("url not in domain list")) {
          reject(new Error("本地接口被微信域名校验拦截，请在开发者工具里关闭“校验合法域名、web-view、TLS 版本以及 HTTPS 证书”。"))
          return
        }
        if (message.includes("timeout")) {
          reject(new Error("请求超时，请确认后端已启动：uvicorn backend.main:app --reload"))
          return
        }
        reject(new Error(message))
      }
    })
  })
}

function auditUrl(url) {
  return request("/geo/url-audit", { url })
}

function auditContent(content) {
  return request("/geo/audit", { content })
}

function analyzeUrl(payload) {
  return request("/geo/analyze", payload)
}

function improvePackage(result, useAi = false) {
  return request("/geo/improve", {
    result,
    use_ai: useAi,
    provider: "openai"
  })
}

function saveVersion(payload) {
  return request("/geo/version/save", payload)
}

function reviewVersion(versionId, action) {
  return request("/geo/version/review", {
    version_id: versionId,
    action
  })
}

function retestTask(payload) {
  return request("/geo/retest", payload)
}

function scheduleRetest(payload) {
  return request("/geo/retest/schedule", payload)
}

function injectVersion(payload) {
  return request("/geo/inject", payload)
}

function getCmsTargets() {
  return request("/cms/targets", {}, "GET")
}

function createPublicationPreview(payload) {
  return request("/cms/publications/preview", payload)
}

function confirmPublication(payload) {
  return request("/cms/publications/confirm", payload)
}

function retryPublication(publicationId) {
  return request(`/cms/publications/${publicationId}/retry`, {}, "POST")
}

function verifyPublication(payload) {
  return request("/cms/publications/verify", payload)
}

function saveFeedback(payload) {
  return request("/geo/feedback", payload)
}

function getHistory() {
  return request("/geo/history", {}, "GET")
}

function getTaskDetail(taskId) {
  return request(`/geo/tasks/${taskId}`, {}, "GET")
}

function exportJson(payload) {
  return request("/geo/export/json", payload)
}

module.exports = {
  auditUrl,
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
  saveFeedback,
  retestTask,
  scheduleRetest,
  getHistory,
  getTaskDetail,
  exportJson
}
