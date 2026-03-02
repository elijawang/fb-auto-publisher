/**
 * API 请求封装
 */
import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// ==================== 账号管理 ====================
export const accountApi = {
  create: (data) => api.post('/accounts/', data),
  list: (params) => api.get('/accounts/', { params }),
  get: (id) => api.get(`/accounts/${id}`),
  update: (id, data) => api.put(`/accounts/${id}`, data),
  delete: (id) => api.delete(`/accounts/${id}`),
  // 主页管理
  addPage: (accountId, data) => api.post(`/accounts/${accountId}/pages`, data),
  listPages: (accountId) => api.get(`/accounts/${accountId}/pages`),
  removePage: (pageId) => api.delete(`/accounts/pages/${pageId}`),
  updatePage: (pageId, data) => api.put(`/accounts/pages/${pageId}`, data),
  fetchPages: (accountId) => api.post(`/accounts/${accountId}/pages/fetch`, {}, { timeout: 120000 }),
}

// ==================== 任务管理 ====================
export const taskApi = {
  create: (data) => api.post('/tasks/', data),
  list: (params) => api.get('/tasks/', { params }),
  get: (id) => api.get(`/tasks/${id}`),
  delete: (id) => api.delete(`/tasks/${id}`),
  uploadVideos: (taskId, formData) => api.post(`/tasks/${taskId}/videos/upload`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 600000, // 10分钟超时
  }),
  schedulePreview: (data) => api.post('/tasks/schedule-preview', data),
}

// ==================== 浏览器管理 ====================
export const browserApi = {
  launch: (accountId) => api.post(`/browser/launch/${accountId}`),
  login: (accountId) => api.post(`/browser/login/${accountId}`),
  confirmAuth: (accountId) => api.post(`/browser/confirm-auth/${accountId}`),
  authStatus: (accountId) => api.get(`/browser/auth-status/${accountId}`),
  close: (accountId) => api.post(`/browser/close/${accountId}`),
  closeAll: () => api.post('/browser/close-all'),
  navigatePage: (accountId, pageUrl) => api.post(`/browser/navigate-page/${accountId}?page_url=${encodeURIComponent(pageUrl)}`),
}

// ==================== 发布执行 ====================
export const publisherApi = {
  execute: (taskId) => api.post(`/publisher/execute/${taskId}`),
  resume: (taskId) => api.post(`/publisher/resume/${taskId}`),
}

// ==================== 日志管理 ====================
export const logApi = {
  list: (params) => api.get('/logs/', { params }),
  summary: (taskId) => api.get(`/logs/summary/${taskId}`),
}

export default api
