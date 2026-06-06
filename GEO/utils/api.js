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

function schedulePublicationVerify(payload) {
  return request("/cms/publications/verify/schedule", payload)
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

function getServicePackages() {
  return request("/geo/service-packages", {}, "GET")
}

function saveExperiment(payload) {
  return request("/geo/experiments", payload)
}

function confirmExperiment(experimentId, payload) {
  return request(`/geo/experiments/${experimentId}/confirm`, payload)
}

function saveAttribution(payload) {
  return request("/geo/attributions", payload)
}

function generateReport(payload) {
  return request("/geo/reports/generate", payload)
}

function confirmReport(reportId, payload) {
  return request(`/geo/reports/${reportId}/confirm`, payload)
}

function exportJson(payload) {
  return request("/geo/export/json", payload)
}

function getMonitoringQueries(taskId) {
  return request(`/geo/monitoring/queries?task_id=${encodeURIComponent(taskId)}`, {}, "GET")
}

function generateMonitoringQueries(payload) {
  return request("/geo/monitoring/queries/generate", payload)
}

function parseMonitoringSources(payload) {
  return request("/geo/monitoring/sources/parse", payload)
}

function getMonitoringSummary(taskId) {
  return request(`/geo/monitoring/summary?task_id=${encodeURIComponent(taskId)}`, {}, "GET")
}

function getSourceMap(taskId) {
  return request(`/geo/monitoring/source-map?task_id=${encodeURIComponent(taskId)}`, {}, "GET")
}

function getMonitoringConnectors(taskId) {
  return request(`/geo/monitoring/connectors?task_id=${encodeURIComponent(taskId)}`, {}, "GET")
}

function saveMonitoringConnector(payload) {
  return request("/geo/monitoring/connectors", payload)
}

function updateMonitoringConnector(connectorId, payload) {
  return request(`/geo/monitoring/connectors/${connectorId}`, payload, "PATCH")
}

function getGapActions(taskId) {
  return request(`/geo/actions?task_id=${encodeURIComponent(taskId)}`, {}, "GET")
}

function bootstrapGapActions(taskId) {
  return request(`/geo/actions/bootstrap?task_id=${encodeURIComponent(taskId)}`, {}, "POST")
}

function saveGapAction(payload) {
  return request("/geo/actions", payload)
}

function updateGapAction(actionId, payload) {
  return request(`/geo/actions/${actionId}`, payload, "PATCH")
}

function createGeoArticle(payload) {
  return request("/geo/articles/create", payload)
}

function updateGeoArticleIndexing(articleId, payload) {
  return request(`/geo/articles/${articleId}/indexing`, payload, "PATCH")
}

function getGeoArticleIndexingChecklist(articleId) {
  return request(`/geo/articles/${articleId}/indexing-checklist`, {}, "GET")
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
  schedulePublicationVerify,
  saveFeedback,
  retestTask,
  scheduleRetest,
  getHistory,
  getTaskDetail,
  getServicePackages,
  saveExperiment,
  confirmExperiment,
  saveAttribution,
  generateReport,
  confirmReport,
  exportJson,
  getMonitoringQueries,
  generateMonitoringQueries,
  parseMonitoringSources,
  getMonitoringSummary,
  getSourceMap,
  getMonitoringConnectors,
  saveMonitoringConnector,
  updateMonitoringConnector,
  getGapActions,
  bootstrapGapActions,
  saveGapAction,
  updateGapAction,
  createGeoArticle,
  updateGeoArticleIndexing,
  getGeoArticleIndexingChecklist
}
