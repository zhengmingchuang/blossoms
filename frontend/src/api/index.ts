import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import type {
  ChatAskBody,
  ChatFileItem,
  ChatMessageItem,
  ChatSessionItem,
  ChatStreamEvent,
  DashboardStats,
  DocumentItem,
  DocumentListResult,
  ExtractionListResult,
  FileFieldsResult,
  FillHistoryResult,
  FillResult,
  FusedResult,
  LoginResult,
  McpTool,
  SystemConfig,
  TaskListResult,
  TaskStats,
  TemplateListResult,
  TemplateUploadResult,
  UploadResult,
  UserProfile,
} from './types'

/** 统一的 axios 实例：空 baseURL 走同源，开发期由 vite proxy 转发到后端 */
const api: AxiosInstance = axios.create({ baseURL: '', timeout: 120_000 })

api.interceptors.request.use(config => {
  const token = sessionStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  response => response,
  error => {
    // 401 统一回登录页；登录接口本身失败要留在原地显示错误
    if (error.response?.status === 401 && !error.config?.url?.includes('/api/auth/login')) {
      sessionStorage.removeItem('token')
      sessionStorage.removeItem('user')
      window.location.href = '/login'
    }
    return Promise.reject(error)
  },
)

/* ------------------------------------------------------------------ 认证 */

export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResult>('/api/auth/login', new URLSearchParams({ username, password })),
  register: (username: string, password: string) =>
    api.post<{ message: string; user_id: number }>('/api/auth/register', { username, password }),
  me: () => api.get<UserProfile>('/api/auth/me'),
  uploadAvatar: (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return api.post<{ avatar: string }>('/api/auth/avatar', formData)
  },
  changePassword: (old_password: string, new_password: string) =>
    api.post<{ message: string }>('/api/auth/change-password', { old_password, new_password }),
  changeNickname: (nickname: string) =>
    api.post<{ message: string }>('/api/auth/change-nickname', { nickname }),
}

/* ------------------------------------------------------------------ 文档 */

export const docApi = {
  upload: (formData: FormData) => api.post<UploadResult>('/api/documents/upload', formData),
  list: (page = 1, pageSize = 20) =>
    api.get<DocumentListResult>('/api/documents/list', { params: { page, page_size: pageSize } }),
  detail: (id: number) => api.get<DocumentItem & { chunks?: unknown[] }>(`/api/documents/${id}`),
  delete: (id: number) => api.delete<{ message: string }>(`/api/documents/${id}`),
  batchDelete: (ids: number[]) => api.post<{ message: string }>('/api/documents/batch-delete', { ids }),
  reparse: (id: number) => api.post<{ message: string }>(`/api/documents/${id}/reparse`),
  extract: (id: number) => api.post<{ message: string }>(`/api/documents/${id}/extract`),
  download: (id: number) => api.get<Blob>(`/api/documents/${id}/download`, { responseType: 'blob' }),
}

/* ------------------------------------------------------------------ 抽取 */

export const extractApi = {
  results: (params: Record<string, unknown> = {}) =>
    api.get<ExtractionListResult>('/api/extraction/results', { params }),
  fused: () => api.get<FusedResult>('/api/extraction/fused'),
  files: () =>
    api.get<{ total: number; files: Array<{ id: number; file_name: string; field_count: number }> }>(
      '/api/extraction/files',
    ),
  fileFields: (fid: number) => api.get<FileFieldsResult>(`/api/extraction/files/${fid}/fields`),
  update: (id: number, value: string) =>
    api.put<{ message: string }>(`/api/extraction/results/${id}`, { field_value: value }),
  batchDelete: (ids: number[]) =>
    api.post<{ message: string }>('/api/extraction/results/batch-delete', { ids }),
}

/* ------------------------------------------------------------------ 模板 */

export const tplApi = {
  upload: (formData: FormData) => api.post<TemplateUploadResult>('/api/templates/upload', formData),
  list: () => api.get<TemplateListResult>('/api/templates/list'),
  /** 填报是长任务，超时单独放宽到 5 分钟 */
  fill: (id: number, sourceFileIds?: number[]) =>
    api.post<FillResult>(
      `/api/templates/${id}/fill`,
      sourceFileIds ? { source_file_ids: sourceFileIds } : null,
      { timeout: 300_000 },
    ),
  mappings: (id: number) =>
    api.get<{ total: number; mappings: Array<Record<string, unknown>> }>(`/api/templates/${id}/mappings`),
  updateRequirement: (id: number, text: string) =>
    api.put<{ message: string }>(`/api/templates/${id}/requirement`, { user_requirement: text }),
  delete: (id: number) => api.delete<{ message: string }>(`/api/templates/${id}`),
  batchDelete: (ids: number[]) => api.post<{ message: string }>('/api/templates/batch-delete', { ids }),
  batchDeleteHistory: (ids: number[]) =>
    api.post<{ message: string }>('/api/templates/history/batch-delete', { ids }),
  download: (filename: string) =>
    api.get<Blob>(`/api/templates/download/${encodeURIComponent(filename)}`, { responseType: 'blob' }),
  /** 按填写任务 ID 下载（推荐，避免中文文件名在 URL 中与库不一致） */
  downloadByTask: (taskId: number) =>
    api.get<Blob>(`/api/templates/history/${taskId}/download`, { responseType: 'blob' }),
  history: (params: Record<string, unknown> = {}) =>
    api.get<FillHistoryResult>('/api/templates/history', { params }),
  deleteHistory: (id: number) => api.delete<{ message: string }>(`/api/templates/history/${id}`),
}

/* ------------------------------------------------------------------ 对话 */

export const chatApi = {
  sessions: () => api.get<ChatSessionItem[]>('/api/chat/sessions'),
  sessionMessages: (id: number) => api.get<ChatMessageItem[]>(`/api/chat/sessions/${id}/messages`),
  deleteSession: (id: number) => api.delete<{ message: string }>(`/api/chat/sessions/${id}`),
  files: (sessionId?: number) =>
    api.get<{ total?: number; files: ChatFileItem[] }>('/api/chat/files', {
      params: sessionId != null ? { session_id: sessionId } : {},
    }),
  uploadFile: (formData: FormData, sessionId?: number) =>
    api.post<ChatFileItem>('/api/chat/upload', formData, {
      params: sessionId != null ? { session_id: sessionId } : {},
    }),
  downloadFile: (fid: number | string) =>
    api.get<Blob>(`/api/chat/files/${fid}/download`, { responseType: 'blob' }),
  ask: (
    message: string,
    history: Array<{ role: string; content: string }> = [],
    sessionId?: number,
    fileId?: number | string,
    fileIds?: Array<number | string>,
  ) => {
    const body: ChatAskBody = { message, history }
    if (sessionId != null) body.session_id = sessionId
    if (fileIds && fileIds.length) body.file_ids = fileIds
    else if (fileId != null) body.file_id = fileId
    return api.post<{ answer: string; session_id: number }>('/api/chat/ask', body)
  },

  /**
   * 流式对话。
   *
   * 用 fetch + ReadableStream 而不是 EventSource：EventSource 只支持 GET、不能带
   * Authorization 头，而这里需要 POST 请求体 + Bearer token。
   *
   * @param body 请求体，字段同 ask
   * @param opts.onEvent 每收到一个 SSE 事件回调一次（session / delta / tool_start / tool_end / done / error）
   * @param opts.signal 用于「停止生成」
   */
  askStream: async (
    body: ChatAskBody,
    opts: { onEvent?: (ev: ChatStreamEvent) => void; signal?: AbortSignal } = {},
  ): Promise<void> => {
    const { onEvent, signal } = opts
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
    let buffer = ''

    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE 以空行分隔事件；网络分片可能把一个事件切成两半，所以只在找到 \n\n 时消费
      let sep = buffer.indexOf('\n\n')
      while (sep >= 0) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        for (const line of raw.split('\n')) {
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (!payload) continue
          try {
            onEvent?.(JSON.parse(payload) as ChatStreamEvent)
          } catch {
            /* 忽略无法解析的心跳或半包 */
          }
        }
        sep = buffer.indexOf('\n\n')
      }
    }
  },
}

/* ------------------------------------------------------------------ 其他 */

export const dashApi = {
  stats: () => api.get<DashboardStats>('/api/dashboard/stats'),
}

export const taskApi = {
  list: (params: Record<string, unknown> = {}) => api.get<TaskListResult>('/api/tasks/list', { params }),
  stats: () => api.get<TaskStats>('/api/tasks/stats'),
  delete: (id: number) => api.delete<{ message: string }>(`/api/tasks/${id}`),
  batchDelete: (ids: number[]) => api.post<{ message: string }>('/api/tasks/batch-delete', { ids }),
}

export const settingsApi = {
  get: () => api.get<SystemConfig>('/api/settings/config'),
  save: (data: SystemConfig) => api.put<{ message: string }>('/api/settings/config', data),
  users: () => api.get<{ total: number; users: UserProfile[] }>('/api/settings/users'),
  updateUser: (id: number, data: Partial<UserProfile>) =>
    api.put<{ message: string }>(`/api/settings/users/${id}`, data),
  deleteUser: (id: number) => api.delete<{ message: string }>(`/api/settings/users/${id}`),
  logs: (params: Record<string, unknown> = {}) =>
    api.get<{ total: number; logs: Array<Record<string, unknown>> }>('/api/settings/logs', { params }),
  fieldDict: () =>
    api.get<{ total: number; fields: Array<Record<string, unknown>> }>('/api/settings/field-dict'),
  addField: (data: Record<string, unknown>) =>
    api.post<{ message: string }>('/api/settings/field-dict', data),
  updateField: (id: number, data: Record<string, unknown>) =>
    api.put<{ message: string }>(`/api/settings/field-dict/${id}`, data),
  deleteField: (id: number) => api.delete<{ message: string }>(`/api/settings/field-dict/${id}`),
}

export const mcpApi = {
  tools: () => api.get<{ tools: McpTool[] }>('/api/mcp/tools'),
  jsonrpc: (body: Record<string, unknown>, config?: AxiosRequestConfig) =>
    api.post<Record<string, unknown>>('/api/mcp/jsonrpc', body, config),
}

export type * from './types'
export default api
