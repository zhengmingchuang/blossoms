/**
 * 后端响应体的类型定义。
 *
 * 字段名与 backend/models.py、backend/routers/*.py 的返回结构保持一致（下划线命名），
 * API 层不做二次转换，因此类型可以直接贴到业务代码里用。
 *
 * 约定：
 * - 时间字段后端已用 fmt_time() 格式化成 `YYYY-MM-DD HH:mm:ss` 字符串，不是 Date。
 * - 后端用 float 表示置信度 / 准确率，取值 0–1，展示时再乘 100。
 */

/** 当前登录用户（GET /api/auth/me） */
export interface UserProfile {
  id: number
  username: string
  nickname: string
  role: string
  avatar: string
}

/** 登录成功返回（POST /api/auth/login） */
export interface LoginResult {
  access_token: string
  token_type: string
  username: string
  nickname: string
  role: string
  avatar: string
}

/* ------------------------------------------------------------------ 文档 */

export type ParseStatus = 'pending' | 'parsing' | 'done' | 'failed'

export interface DocumentItem {
  id: number
  file_name: string
  file_type: string
  file_size: number
  parse_status: ParseStatus | string
  parse_time: number
  chunk_count: number
  field_count: number
  upload_time: string
}

export interface DocumentListResult {
  total: number
  page: number
  files: DocumentItem[]
}

export interface UploadResult {
  message: string
  files: Array<Partial<DocumentItem> & { id: number; file_name: string }>
}

/* ------------------------------------------------------------------ 抽取 */

export interface ExtractionItem {
  id: number
  file_id: number
  file_name: string
  field_name: string
  field_value: string
  confidence: number
  extract_method: string
  source_position: string
  create_time: string
}

export interface ExtractionListResult {
  total: number
  page: number
  page_size: number
  results: ExtractionItem[]
}

/** 单个文件的字段明细（GET /api/extraction/files/{id}/fields） */
export interface FileFieldsResult {
  file_id: number
  file_name: string
  total: number
  fields: Array<Pick<ExtractionItem, 'field_name' | 'field_value' | 'confidence' | 'extract_method'>>
}

/** 多源融合结果（GET /api/extraction/fused） */
export interface FusedResult {
  total: number
  source_files: number
  fused_results: Array<Record<string, unknown>>
}

/* ------------------------------------------------------------------ 模板 */

export type TemplateMode = 'table_fill' | 'field_fill'

export interface TemplateItem {
  id: number
  template_name: string
  template_type: string
  template_mode: TemplateMode | string
  user_requirement: string
  field_count: number
  upload_time: string
}

export interface TemplateListResult {
  total: number
  templates: TemplateItem[]
}

/** 上传模板后的结构探测结果（POST /api/templates/upload） */
export interface TemplateUploadResult {
  message: string
  template_id: number
  template_name: string
  user_requirement: string
  mode: TemplateMode | string
  field_count: number
  columns: string[]
  fields: Array<{ field_name: string; location_type: string }>
  multi_sheet: boolean
  sheet_count: number
}

/** 填报结果（POST /api/templates/{id}/fill） */
export interface FillResult {
  message: string
  template_id: number
  mode: TemplateMode | string
  field_count: number
  filled_count: number
  total_rows: number
  accuracy: number | null
  output_file: string
  source_file?: string
  details?: unknown
}

/** 填报历史条目（GET /api/templates/history） */
export interface FillHistoryItem {
  id: number
  task_name: string
  status: string
  accuracy: number | null
  elapsed: number | null
  output_file: string
  start_time: string
  end_time: string
  exists?: boolean
}

export interface FillHistoryResult {
  total: number
  page: number
  history: FillHistoryItem[]
}

/* ------------------------------------------------------------------ 任务 */

export interface TaskItem {
  id: number
  task_type: string
  task_name: string
  status: string
  elapsed: number | null
  accuracy: number | null
  result_summary: string
  start_time: string
  end_time: string
}

export interface TaskListResult {
  total: number
  page: number
  tasks: TaskItem[]
}

export interface TaskStats {
  total: number
  done: number
  running: number
  error: number
  pending: number
}

/* ------------------------------------------------------------------ 对话 */

export interface ChatSessionItem {
  id: number
  title: string
  updated_at: string
}

export interface ChatMessageItem {
  id: number
  role: 'user' | 'assistant' | 'system' | string
  content: string
  create_time: string
}

/** 会话可引用的文件（上传件或填报产物） */
export interface ChatFileItem {
  id: number | string
  name: string
  type: string
  source: 'upload' | 'filled' | 'image' | string
  status?: string
  task_id?: number
  path?: string
}

/** Agent 一次工具调用的记录，用于前端回显执行步骤 */
export interface ToolAction {
  tool: string
  input?: Record<string, unknown>
  ok?: boolean
  [key: string]: unknown
}

export interface ChatAskBody {
  message: string
  history?: Array<{ role: string; content: string }>
  session_id?: number
  file_id?: number | string
  file_ids?: Array<number | string>
}

/** 流式接口的 SSE 事件（POST /api/chat/ask/stream） */
export type ChatStreamEvent =
  | { type: 'session'; session_id: number }
  | { type: 'delta'; text: string }
  | { type: 'tool_start'; tool: string; input?: Record<string, unknown> }
  | { type: 'tool_end'; tool: string; ok: boolean }
  | {
      type: 'done'
      session_id: number
      answer: string
      tool_actions?: ToolAction[] | null
      edited_files?: Array<{ id: number; name: string; type: string }> | null
    }
  | { type: 'error'; message: string }

/* ------------------------------------------------------------------ 其他 */

export interface DashboardStats {
  [key: string]: unknown
}

export interface SystemConfig {
  [key: string]: unknown
}

export interface McpTool {
  name: string
  description?: string
  input_schema?: Record<string, unknown>
  [key: string]: unknown
}

/** 后端统一的错误体（FastAPI HTTPException） */
export interface ApiErrorBody {
  detail?: string
  message?: string
}
