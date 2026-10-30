import axios from 'axios'

const api = axios.create({ baseURL: '', timeout: 120000 })

api.interceptors.request.use(c => {
  const t = sessionStorage.getItem('token')
  if (t) c.headers.Authorization = `Bearer ${t}`
  return c
})

api.interceptors.response.use(r => r, err => {
  if (err.response?.status === 401 && !err.config?.url?.includes('/api/auth/login')) {
    sessionStorage.removeItem('token')
    sessionStorage.removeItem('user')
    window.location.href = '/login'
  }
  return Promise.reject(err)
})

export const authApi = {
  login: (username, password) => api.post('/api/auth/login', new URLSearchParams({ username, password })),
  register: (username, password) => api.post('/api/auth/register', { username, password }),
  me: () => api.get('/api/auth/me'),
  uploadAvatar: (file) => { const fd = new FormData(); fd.append('file', file); return api.post('/api/auth/avatar', fd) },
  changePassword: (old_password, new_password) => api.post('/api/auth/change-password', { old_password, new_password }),
  changeNickname: (nickname) => api.post('/api/auth/change-nickname', { nickname }),
}

export const docApi = {
  upload: formData => api.post('/api/documents/upload', formData),
  list: (page = 1, pageSize = 20) => api.get('/api/documents/list', { params: { page, page_size: pageSize } }),
  detail: id => api.get(`/api/documents/${id}`),
  delete: id => api.delete(`/api/documents/${id}`),
  batchDelete: ids => api.post('/api/documents/batch-delete', { ids }),
  reparse: id => api.post(`/api/documents/${id}/reparse`),
  extract: id => api.post(`/api/documents/${id}/extract`),
  download: id => api.get(`/api/documents/${id}/download`, { responseType: 'blob' }),
}

export const extractApi = {
  results: (params = {}) => api.get('/api/extraction/results', { params }),
  fused: () => api.get('/api/extraction/fused'),
  files: () => api.get('/api/extraction/files'),
  fileFields: fid => api.get(`/api/extraction/files/${fid}/fields`),
  update: (id, value) => api.put(`/api/extraction/results/${id}`, { field_value: value }),
  batchDelete: ids => api.post('/api/extraction/results/batch-delete', { ids }),
}

export const tplApi = {
  upload: formData => api.post('/api/templates/upload', formData),
  list: () => api.get('/api/templates/list'),
  fill: (id, sourceFileIds) => api.post(`/api/templates/${id}/fill`, sourceFileIds ? { source_file_ids: sourceFileIds } : null, { timeout: 300000 }),
  mappings: id => api.get(`/api/templates/${id}/mappings`),
  updateRequirement: (id, text) => api.put(`/api/templates/${id}/requirement`, { user_requirement: text }),
  delete: id => api.delete(`/api/templates/${id}`),
  batchDelete: ids => api.post('/api/templates/batch-delete', { ids }),
  batchDeleteHistory: ids => api.post('/api/templates/history/batch-delete', { ids }),
  download: filename =>
    api.get(`/api/templates/download/${encodeURIComponent(filename)}`, { responseType: 'blob' }),
  /** 按填写任务 ID 下载（推荐，避免中文文件名在 URL 中与库不一致） */
  downloadByTask: taskId =>
    api.get(`/api/templates/history/${taskId}/download`, { responseType: 'blob' }),
  history: (params = {}) => api.get('/api/templates/history', { params }),
  deleteHistory: id => api.delete(`/api/templates/history/${id}`),
}

export const chatApi = {
  sessions: () => api.get('/api/chat/sessions'),
  sessionMessages: id => api.get(`/api/chat/sessions/${id}/messages`),
  deleteSession: id => api.delete(`/api/chat/sessions/${id}`),
  files: (sessionId) => api.get('/api/chat/files', { params: sessionId != null ? { session_id: sessionId } : {} }),
  uploadFile: (formData, sessionId) => api.post('/api/chat/upload', formData, { params: sessionId != null ? { session_id: sessionId } : {} }),
  downloadFile: fid => api.get(`/api/chat/files/${fid}/download`, { responseType: 'blob' }),
  ask: (message, history = [], sessionId, fileId, fileIds) => {
    const body = { message, history }
    if (sessionId != null) body.session_id = sessionId
    if (fileIds && fileIds.length) body.file_ids = fileIds
    else if (fileId != null) body.file_id = fileId
    return api.post('/api/chat/ask', body)
  },
  /**
   * 流式对话。用 fetch + ReadableStream 读取 SSE（EventSource 不支持 POST + 自定义头）。
   * @param {object} body 请求体，字段同 ask
   * @param {{ onEvent?: (ev: object) => void, signal?: AbortSignal }} opts
   */
  askStream: async (body, { onEvent, signal } = {}) => {
    const token = sessionStorage.getItem('token')
    const resp = await fetch('/api/chat/ask/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    })
    if (resp.status === 401) {
      sessionStorage.removeItem('token')
      sessionStorage.removeItem('user')
      window.location.href = '/login'
      throw new Error('未登录')
    }
    if (!resp.ok || !resp.body) throw new Error(`请求失败（HTTP ${resp.status}）`)

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let sep
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, sep)
        buf = buf.slice(sep + 2)
        for (const line of raw.split('\n')) {
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (!payload) continue
          try {
            onEvent?.(JSON.parse(payload))
          } catch {
            /* 忽略无法解析的心跳或半包 */
          }
        }
      }
    }
  },
}

export const dashApi = {
  stats: () => api.get('/api/dashboard/stats'),
}

export const taskApi = {
  list: (params = {}) => api.get('/api/tasks/list', { params }),
  stats: () => api.get('/api/tasks/stats'),
  delete: id => api.delete(`/api/tasks/${id}`),
  batchDelete: ids => api.post('/api/tasks/batch-delete', { ids }),
}

export const settingsApi = {
  get: () => api.get('/api/settings/config'),
  save: data => api.put('/api/settings/config', data),
  users: () => api.get('/api/settings/users'),
  updateUser: (id, data) => api.put(`/api/settings/users/${id}`, data),
  deleteUser: id => api.delete(`/api/settings/users/${id}`),
  logs: (params = {}) => api.get('/api/settings/logs', { params }),
  fieldDict: () => api.get('/api/settings/field-dict'),
  addField: data => api.post('/api/settings/field-dict', data),
  updateField: (id, data) => api.put(`/api/settings/field-dict/${id}`, data),
  deleteField: id => api.delete(`/api/settings/field-dict/${id}`),
}

export const mcpApi = {
  tools: () => api.get('/api/mcp/tools'),
  jsonrpc: body => api.post('/api/mcp/jsonrpc', body),
}

export default api
