const API_BASE_BY_ENV = {
  develop: "http://127.0.0.1:8000",
  trial: "https://staging.geo.example.com",
  release: "https://api.geo.example.com"
}

function resolveRuntimeEnv() {
  try {
    return wx.getAccountInfoSync().miniProgram.envVersion || "develop"
  } catch (error) {
    return "develop"
  }
}

function resolveApiBase(runtimeEnv) {
  const override = wx.getStorageSync("geoApiBaseOverride")
  if (override) {
    return override
  }
  return API_BASE_BY_ENV[runtimeEnv] || API_BASE_BY_ENV.develop
}

const runtimeEnv = resolveRuntimeEnv()

App({
  globalData: {
    runtimeEnv,
    apiBase: resolveApiBase(runtimeEnv),
    apiKey: wx.getStorageSync("geoApiKey") || "",
    workspaceId: wx.getStorageSync("geoWorkspaceId") || "",
    customerId: wx.getStorageSync("geoCustomerId") || ""
  }
})
