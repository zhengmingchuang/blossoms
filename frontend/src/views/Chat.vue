<template>
  <div class="chat-page">
    <div class="page-header page-header-row">
      <div>
        <h2>智能交互</h2>
        <p>基于文档的智能问答与任务执行</p>
      </div>
      <el-button class="history-toggle mobile-only" @click="historyDrawerOpen = true">历史对话</el-button>
    </div>

    <div class="chat-layout">
      <aside class="chat-sidebar desktop-only card card-animate">
        <div class="sidebar-head">
          <span class="sidebar-title">历史对话</span>
          <el-button type="primary" size="small" plain @click="newChat">新对话</el-button>
        </div>
        <div class="session-list" v-loading="sessionsLoading">
          <div
            v-for="s in sessions"
            :key="s.id"
            :class="['session-item', { active: currentSessionId === s.id }]"
            @click="openSession(s.id)"
          >
            <div class="session-item-main">
              <div class="session-title">{{ s.title }}</div>
              <div class="session-meta">{{ s.updated_at }}</div>
            </div>
            <el-button
              class="session-del"
              type="danger"
              link
              :icon="Delete"
              @click.stop="removeSession(s)"
            />
          </div>
          <div v-if="!sessions.length && !sessionsLoading" class="session-empty">暂无历史记录</div>
        </div>
        <div v-if="editedDocHistory.length" class="edited-docs-section">
          <div class="edited-docs-head">
            <span class="edited-docs-title">📦 文档修改记录</span>
            <el-button type="danger" size="small" link @click="clearDocHistory" style="font-size:11px">清空</el-button>
          </div>
          <div class="edited-docs-list">
            <div v-for="(doc, idx) in editedDocHistory" :key="doc.id + '-' + doc.time + '-' + idx" class="edited-doc-item">
              <div class="edited-doc-info">
                <span class="edited-doc-name" :title="doc.name">📄 {{ doc.name }}</span>
                <span class="edited-doc-time">{{ doc.time }}</span>
              </div>
              <div class="edited-doc-actions">
                <el-button type="primary" size="small" link :icon="Download" @click="downloadFile(doc)">下载</el-button>
                <el-button type="danger" size="small" link :icon="Delete" @click="removeDocHistory(idx)" />
              </div>
            </div>
          </div>
        </div>
      </aside>

      <div class="chat-main card card-animate">
        <div class="messages" ref="msgBox">
          <div v-if="!messages.length" class="welcome">
            <div class="welcome-icon">
              <div class="welcome-icon-inner">💡</div>
            </div>
            <h3>你好，有什么可以帮你？</h3>
            <p>我可以帮你查询文档内容、提取信息、填写模板、执行分析任务</p>
            <div class="cmd-groups">
              <div class="cmd-group" v-for="g in cmdGroups" :key="g.title">
                <div class="cmd-group-title">{{ g.icon }} {{ g.title }}</div>
                <div class="qq" v-for="c in g.cmds" :key="c" @click="input = c; send()">{{ c }}</div>
              </div>
            </div>
          </div>

          <div v-for="(m, i) in messages" :key="i + '-' + (m.create_time || '') + m.role" :class="['msg', m.role]">
            <div class="msg-avatar">
              <img :src="m.role === 'user' ? userAvatar : '/cs-avatar.jpg'" class="msg-avatar-img" @error="e => e.target.style.display='none'" />
              <span class="msg-avatar-fallback">{{ m.role === 'user' ? userInitial : 'AI' }}</span>
            </div>
            <div class="msg-content">
              <div v-if="m.toolActions && m.toolActions.length" class="tool-actions-panel">
                <div class="tool-actions-title">📋 处理步骤</div>
                <div v-for="(ta, ti) in m.toolActions" :key="ti" class="tool-action-item">
                  <span class="tool-action-name">{{ toolLabel(ta.tool) }}</span>
                  <span class="tool-action-detail" v-if="ta.input">{{ toolInputSummary(ta) }}</span>
                  <span v-if="ta.pending" class="tool-action-state">执行中…</span>
                  <span v-else-if="ta.ok === false" class="tool-action-state is-fail">失败</span>
                </div>
                <div class="tool-actions-done" v-if="!m.streaming">处理完成</div>
              </div>
              <div class="msg-bubble" :class="{ 'is-streaming': m.streaming }">
                <template v-if="m.role === 'assistant'">
                  <div v-if="m.streaming && !m.content" class="typing">
                    <span></span><span></span><span></span>
                  </div>
                  <div v-else v-html="renderMd(m.content)" class="md-body"></div>
                </template>
                <div v-else>{{ m.content }}</div>
              </div>
              <div v-if="m.editedFiles && m.editedFiles.length" class="download-panel">
                <div class="download-panel-title">📄 已修改的文档</div>
                <div v-for="ef in m.editedFiles" :key="ef.id" class="download-item">
                  <span class="download-file-icon">📎</span>
                  <span class="download-file-name">{{ ef.name }}</span>
                  <el-button type="primary" size="small" @click="downloadFile(ef)">下载文件</el-button>
                </div>
              </div>
            </div>
          </div>

        </div>

        <div class="input-area">
          <div class="input-row file-row">
            <el-select
              v-model="selectedFileIds"
              placeholder="选择操作文档（可多选）"
              clearable
              multiple
              collapse-tags
              collapse-tags-tooltip
              size="small"
              class="file-select"
              filterable
              @focus="loadFiles"
            >
              <el-option-group label="已上传文档/图片">
                <el-option v-for="f in uploadFiles" :key="f.id" :label="f.name" :value="f.id">
                  <span style="font-size:13px">{{ f.name }}</span>
                  <el-tag size="small" :type="f.source === 'image' ? 'warning' : 'info'" style="margin-left:6px">.{{ f.type }}</el-tag>
                </el-option>
              </el-option-group>
              <el-option-group v-if="filledFiles.length" label="已填写的文档">
                <el-option v-for="f in filledFiles" :key="f.id" :label="f.name" :value="f.id">
                  <span style="font-size:13px">{{ f.name }}</span>
                  <el-tag size="small" type="success" style="margin-left:6px">.{{ f.type }}</el-tag>
                </el-option>
              </el-option-group>
            </el-select>
            <el-upload :http-request="chatUpload" :show-file-list="false" accept=".docx,.doc,.md,.xlsx,.xls,.txt,.png,.jpg,.jpeg,.gif,.webp,.bmp" multiple class="chat-upload-btn">
              <el-button size="small" :icon="UploadFilled" :loading="uploading">上传文档/图片</el-button>
            </el-upload>
          </div>
          <div class="input-row">
            <el-input
              v-model="input"
              placeholder="输入你的问题或指令..."
              @keyup.enter="send"
              class="chat-input"
            />
            <el-button v-if="!sending" type="primary" :icon="Promotion" @click="send" class="send-btn">
              发送
            </el-button>
            <el-button v-else type="danger" :icon="VideoPause" @click="stop" class="send-btn">
              停止
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <el-drawer v-model="historyDrawerOpen" title="历史对话" direction="ltr" size="300px" class="chat-history-drawer">
      <div class="drawer-actions">
        <el-button type="primary" size="small" @click="newChat">新对话</el-button>
      </div>
      <div class="session-list drawer-sessions" v-loading="sessionsLoading">
        <div
          v-for="s in sessions"
          :key="'d-' + s.id"
          :class="['session-item', { active: currentSessionId === s.id }]"
          @click="openSession(s.id)"
        >
          <div class="session-item-main">
            <div class="session-title">{{ s.title }}</div>
            <div class="session-meta">{{ s.updated_at }}</div>
          </div>
          <el-button
            class="session-del"
            type="danger"
            link
            :icon="Delete"
            @click.stop="removeSession(s)"
          />
        </div>
        <div v-if="!sessions.length && !sessionsLoading" class="session-empty">暂无历史记录</div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { chatApi } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Promotion, Delete, Download, UploadFilled, VideoPause } from '@element-plus/icons-vue'
import { marked } from 'marked'

const userAvatar = computed(() => {
  try {
    const a = JSON.parse(sessionStorage.getItem('user') || '{}').avatar
    if (a) return a
  } catch {}
  return '/default-avatar.jpg'
})

const userInitial = computed(() => {
  try {
    const u = JSON.parse(sessionStorage.getItem('user') || '{}')
    return ((u.nickname || u.username || 'U')[0]).toUpperCase()
  } catch { return 'U' }
})

const messages = ref([])
const input = ref('')
const sending = ref(false)
const msgBox = ref(null)
const sessions = ref([])
const sessionsLoading = ref(false)
const currentSessionId = ref(null)
const historyDrawerOpen = ref(false)
const selectedFileIds = ref([])
const fileList = ref([])
const uploading = ref(false)
const editedDocHistory = ref(JSON.parse(localStorage.getItem('editedDocHistory') || '[]'))

const _saveDocHistory = () => {
  const keep = editedDocHistory.value.slice(0, 50)
  localStorage.setItem('editedDocHistory', JSON.stringify(keep))
}

const addToDocHistory = (ef) => {
  const now = new Date()
  const timeStr = `${now.getMonth()+1}/${now.getDate()} ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`
  editedDocHistory.value.unshift({ id: ef.id, name: ef.name, type: ef.type, time: timeStr })
  _saveDocHistory()
}

const removeDocHistory = (idx) => {
  editedDocHistory.value.splice(idx, 1)
  _saveDocHistory()
}

const clearDocHistory = async () => {
  try {
    await ElMessageBox.confirm('确定清空所有文档修改记录？', '清空记录', { type: 'warning' })
  } catch { return }
  editedDocHistory.value = []
  localStorage.removeItem('editedDocHistory')
}

const uploadFiles = computed(() => fileList.value.filter(f => f.source === 'upload' || f.source === 'image'))
const filledFiles = computed(() => fileList.value.filter(f => f.source === 'filled'))

const cmdGroups = [
  { icon: '📄', title: '文档查询', cmds: ['上传的文档里有哪些内容？', '帮我查一下GDP相关的数据', '总结一下文档的主要内容'] },
  { icon: '📊', title: '数据提取', cmds: ['提取文档中的关键指标', '从文件中提取所有人名和日期', '查看已提取的字段结果'] },
  { icon: '✏️', title: '文档编辑', cmds: ['把文档中的"旧文本"替换为"新文本"', '在文档末尾追加一段说明', '删除文档中包含"废弃"的段落'] },
  { icon: '🎨', title: '格式调整', cmds: ['把标题设为黑体加粗居中', '全文字号改为12磅宋体', '将正文行距调整为1.5倍'] },
  { icon: '🧩', title: '智能填表', cmds: ['把数据源按模板格式智能填充', '根据模板生成已填充的表格', '帮我把数据填到模板里，自动算总和与平均值'] },
]

const toolSuccess = (ta) => {
  try {
    if (!ta.result) return true
    const r = typeof ta.result === 'string' ? JSON.parse(ta.result) : ta.result
    return !r.error
  } catch { return true }
}

const toolLabel = (name) => {
  const map = {
    search_documents: '检索文档内容',
    extract_info: '提取关键信息',
    list_files: '查询文件列表',
    edit_document: '编辑文档内容',
    analyze_document: '分析文档结构',
    format_document: '调整文档格式',
    create_spreadsheet: '创建Excel表格',
    format_spreadsheet: '格式化Excel',
    add_chart: '创建图表',
    validate_spreadsheet: '设置数据有效性',
    fill_template_smart: '智能模板填表',
    get_extract_results: '获取提取结果',
  }
  return map[name] || '处理中'
}

const _findFileName = (fileId) => {
  const f = fileList.value.find(x => x.id === fileId)
  return f ? f.name : ''
}

const toolInputSummary = (ta) => {
  try {
    const inp = ta.input || {}
    const fname = inp.file_id ? _findFileName(inp.file_id) : ''
    const fnameStr = fname ? `「${fname}」` : ''

    if (ta.tool === 'search_documents') return `关键词："${inp.query || ''}"`
    if (ta.tool === 'extract_info') return fnameStr ? `从${fnameStr}中提取` : ''
    if (ta.tool === 'edit_document') {
      const cnt = (inp.operations || []).length
      return fnameStr ? `${fnameStr}，${cnt}项修改` : `${cnt}项修改`
    }
    if (ta.tool === 'format_document') {
      const parts = []
      if (inp.style_target) {
        const stMap = { heading: '标题', heading_1: '一级标题', heading_2: '二级标题', heading_3: '三级标题', body: '正文', all: '全文' }
        parts.push(stMap[inp.style_target] || inp.style_target)
      }
      if (inp.font_name) parts.push(inp.font_name)
      if (inp.font_size) parts.push(`${inp.font_size}磅`)
      if (inp.bold) parts.push('加粗')
      if (inp.alignment) {
        const alMap = { center: '居中', left: '左对齐', right: '右对齐', justify: '两端对齐' }
        parts.push(alMap[inp.alignment] || inp.alignment)
      }
      const detail = parts.length ? parts.join('、') : ''
      if (fnameStr && detail) return `${fnameStr}：${detail}`
      return fnameStr || detail
    }
    if (ta.tool === 'get_extract_results') return fnameStr ? `查看${fnameStr}的提取结果` : ''
    if (ta.tool === 'fill_template_smart') {
      const tplName = inp.template_file_id ? _findFileName(inp.template_file_id) : ''
      const dataName = inp.data_file_id ? _findFileName(inp.data_file_id) : ''
      const parts = []
      if (tplName) parts.push(`模板「${tplName}」`)
      if (dataName) parts.push(`数据源「${dataName}」`)
      return parts.join(' + ')
    }
    return ''
  } catch { return '' }
}

const renderMd = t => { try { return marked.parse(t || '') } catch { return t } }
const scrollBottom = () => nextTick(() => { if (msgBox.value) msgBox.value.scrollTop = msgBox.value.scrollHeight })

const loadFiles = async () => {
  try {
    const { data } = await chatApi.files()
    fileList.value = Array.isArray(data) ? data : []
  } catch { fileList.value = [] }
}

const chatUpload = async ({ file }) => {
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    const { data } = await chatApi.uploadFile(fd)
    const isImg = data.source === 'image'
    ElMessage.success(`"${data.name}" 已上传${isImg ? '' : '，正在解析...'}`)
    if (!isImg) setTimeout(loadFiles, 3000)
    await loadFiles()
    if (!selectedFileIds.value.includes(data.id)) {
      selectedFileIds.value = [...selectedFileIds.value, data.id]
    }
  } catch {
    ElMessage.error('上传失败')
  } finally { uploading.value = false }
}

const downloadFile = async (ef) => {
  try {
    const { data } = await chatApi.downloadFile(ef.id)
    const url = URL.createObjectURL(data)
    const a = document.createElement('a')
    a.href = url
    a.download = ef.name
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('下载成功')
  } catch {
    ElMessage.error('下载失败')
  }
}

const loadSessions = async () => {
  sessionsLoading.value = true
  try {
    const { data } = await chatApi.sessions()
    sessions.value = Array.isArray(data) ? data : []
  } catch {
    sessions.value = []
  } finally {
    sessionsLoading.value = false
  }
}

const newChat = () => {
  messages.value = []
  currentSessionId.value = null
  historyDrawerOpen.value = false
}

const openSession = async (id) => {
  currentSessionId.value = id
  historyDrawerOpen.value = false
  try {
    const { data } = await chatApi.sessionMessages(id)
    const list = Array.isArray(data) ? data : []
    messages.value = list.map(m => ({ role: m.role, content: m.content || '', create_time: m.create_time }))
    scrollBottom()
  } catch {
    ElMessage.error('加载对话失败')
  }
}

const removeSession = async (s) => {
  try {
    await ElMessageBox.confirm(`确定删除「${s.title}」？`, '删除对话', { type: 'warning' })
  } catch {
    return
  }
  try {
    await chatApi.deleteSession(s.id)
    if (currentSessionId.value === s.id) newChat()
    await loadSessions()
    ElMessage.success('已删除')
  } catch {
    ElMessage.error('删除失败')
  }
}

let streamCtrl = null

const stop = () => {
  if (streamCtrl) streamCtrl.abort()
}

const send = async () => {
  const q = input.value.trim()
  if (!q || sending.value) return
  const selFiles = fileList.value.filter(f => selectedFileIds.value.includes(f.id))
  const prefix = selFiles.length ? `[${selFiles.map(f => f.name).join(', ')}] ` : ''
  const displayQ = prefix + q

  // 历史只取当前提问之前的部分，后端会自行追加本轮用户消息
  const history = messages.value.map(m => ({ role: m.role, content: m.content }))

  messages.value.push({ role: 'user', content: displayQ })
  input.value = ''
  sending.value = true

  messages.value.push({ role: 'assistant', content: '', toolActions: [], streaming: true, editedFiles: null })
  const aiMsg = messages.value[messages.value.length - 1]
  scrollBottom()

  streamCtrl = new AbortController()
  try {
    await chatApi.askStream(
      {
        message: q,
        history: history.slice(-10),
        session_id: currentSessionId.value,
        file_ids: selectedFileIds.value,
      },
      {
        signal: streamCtrl.signal,
        onEvent: ev => {
          if (ev.type === 'session') {
            if (ev.session_id != null) currentSessionId.value = ev.session_id
          } else if (ev.type === 'delta') {
            aiMsg.content += ev.text
            scrollBottom()
          } else if (ev.type === 'tool_start') {
            aiMsg.toolActions.push({ tool: ev.tool, input: ev.input, pending: true })
            scrollBottom()
          } else if (ev.type === 'tool_end') {
            const running = [...aiMsg.toolActions].reverse().find(t => t.tool === ev.tool && t.pending)
            if (running) {
              running.pending = false
              running.ok = ev.ok
            }
            scrollBottom()
          } else if (ev.type === 'done') {
            if (ev.session_id != null) currentSessionId.value = ev.session_id
            if (!aiMsg.content && ev.answer) aiMsg.content = ev.answer
            if (ev.edited_files && ev.edited_files.length) {
              aiMsg.editedFiles = ev.edited_files
              ev.edited_files.forEach(ef => addToDocHistory(ef))
            }
          } else if (ev.type === 'error') {
            aiMsg.content = aiMsg.content || (ev.message || '处理失败，请稍后重试。')
          }
        },
      }
    )
    if (!aiMsg.content) aiMsg.content = '操作已完成。'
  } catch (e) {
    if (e?.name === 'AbortError') {
      aiMsg.content = aiMsg.content || '已停止生成。'
    } else {
      aiMsg.content = aiMsg.content || '请求失败，请检查后端服务和LLM配置。'
    }
  } finally {
    aiMsg.streaming = false
    aiMsg.toolActions.forEach(t => { t.pending = false })
    sending.value = false
    streamCtrl = null
    scrollBottom()
    loadSessions()
  }
}

onMounted(() => { loadSessions(); loadFiles() })
</script>

<style scoped>
.chat-page { height: 100%; display: flex; flex-direction: column; }

.page-header-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.history-toggle { flex-shrink: 0; }

.mobile-only { display: none; }
.desktop-only { display: flex; }

@media (max-width: 767px) {
  .mobile-only { display: inline-flex; }
  .desktop-only { display: none !important; }
}

.chat-layout {
  display: flex;
  gap: 16px;
  flex: 1;
  min-height: 0;
  align-items: stretch;
}

.chat-sidebar {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  height: calc(100vh - 180px);
}

.sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--card-border);
}

.sidebar-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-color);
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  min-height: 0;
  scrollbar-width: thin;
  scrollbar-color: rgba(0,0,0,0.15) transparent;
}
.session-list::-webkit-scrollbar {
  width: 4px;
}
.session-list::-webkit-scrollbar-thumb {
  background: rgba(0,0,0,0.15);
  border-radius: 4px;
}
.session-list::-webkit-scrollbar-thumb:hover {
  background: rgba(0,0,0,0.25);
}

.session-item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 10px 10px;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s;
  margin-bottom: 4px;
  border: 1px solid transparent;
}

.session-item:hover {
  background: rgba(255, 255, 255, 0.35);
}

.session-item.active {
  background: rgba(255, 255, 255, 0.5);
  border-color: var(--card-border);
}

.session-item-main {
  flex: 1;
  min-width: 0;
}

.session-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-color);
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.session-meta {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
}

.session-del { flex-shrink: 0; padding: 4px !important; }

.session-empty {
  font-size: 13px;
  color: var(--text-secondary);
  text-align: center;
  padding: 24px 12px;
}

.chat-main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  height: calc(100vh - 180px);
  padding: 0;
  overflow: hidden;
}

.drawer-actions {
  margin-bottom: 12px;
}

.drawer-sessions {
  max-height: calc(100vh - 120px);
}

/* 消息区 */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 欢迎屏 */
.welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  text-align: center;
  padding: 40px 20px;
  gap: 8px;
}

.welcome-icon {
  width: 72px; height: 72px;
  border-radius: 20px;
  background: var(--card-bg);
  backdrop-filter: var(--card-backdrop);
  border: 1px solid var(--card-border);
  display: flex; align-items: center; justify-content: center;
  margin-bottom: 8px;
  box-shadow: var(--card-shadow);
}
.welcome-icon-inner { font-size: 32px; }

.welcome h3 { font-size: 20px; font-weight: 700; color: var(--text-color); margin-top: 4px; }
.welcome p { font-size: 14px; color: var(--text-secondary); max-width: 400px; }

.cmd-groups {
  display: flex;
  gap: 16px;
  margin-top: 20px;
  flex-wrap: wrap;
  justify-content: center;
}

.cmd-group {
  background: var(--card-bg);
  backdrop-filter: var(--card-backdrop);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 14px 16px;
  min-width: 200px;
  text-align: left;
}

.cmd-group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.qq {
  font-size: 13px;
  color: var(--text-color);
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.2s;
  margin-bottom: 4px;
}
.qq:hover { background: rgba(255,255,255,0.5); }

/* 消息气泡 */
.msg {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  animation: msg-in 0.3s cubic-bezier(0.25, 0.8, 0.25, 1) both;
}

@keyframes msg-in {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.msg.user { flex-direction: row-reverse; }

.msg-avatar {
  width: 36px; height: 36px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--palette-1), var(--palette-2));
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
  position: relative;
  box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
.msg.assistant .msg-avatar { background: linear-gradient(135deg, var(--palette-3), var(--palette-4)); }
.msg-avatar-img { width: 100%; height: 100%; object-fit: cover; position: absolute; inset: 0; }
.msg-avatar-fallback { font-size: 13px; font-weight: 600; color: #fff; }

.msg-content { max-width: 70%; }

.msg-bubble {
  background: var(--card-bg);
  backdrop-filter: var(--card-backdrop);
  border: 1px solid var(--card-border);
  border-radius: 16px;
  padding: 12px 16px;
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-color);
  box-shadow: var(--card-shadow);
}

.msg.user .msg-bubble {
  background: linear-gradient(135deg, var(--primary-color), var(--accent-color));
  border-color: transparent;
  color: #fff;
}

/* 打字动画 */
.typing {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 14px 18px;
}
.typing span {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--primary-color);
  animation: typing-bounce 1.2s infinite;
}
.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }

@keyframes typing-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-6px); opacity: 1; }
}

/* Markdown 样式 */
.md-body :deep(p) { margin: 0 0 8px; }
.md-body :deep(p:last-child) { margin-bottom: 0; }
.md-body :deep(code) {
  background: rgba(255,255,255,0.4);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
  font-family: 'Fira Code', monospace;
}
.md-body :deep(pre) {
  background: rgba(0,0,0,0.06);
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 8px 0;
}
.md-body :deep(ul), .md-body :deep(ol) { padding-left: 20px; margin: 4px 0; }
.md-body :deep(li) { margin-bottom: 4px; }

/* 流式输出光标：挂在最后一段内容尾部，避免整块重排 */
.msg-bubble.is-streaming .md-body :deep(> :last-child)::after {
  content: '';
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 3px;
  vertical-align: -0.12em;
  background: var(--primary-color);
  border-radius: 1px;
  animation: caret-blink 1s steps(2, start) infinite;
}
@keyframes caret-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

/* 工具执行状态 */
.tool-action-state {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  animation: state-pulse 1.4s ease-in-out infinite;
}
.tool-action-state.is-fail {
  color: #e24b4a;
  animation: none;
}
@keyframes state-pulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 1; }
}

/* 处理步骤面板 */
.tool-actions-panel {
  background: linear-gradient(135deg, rgba(59, 130, 246, 0.04), rgba(16, 185, 129, 0.04));
  border: 1px solid rgba(59, 130, 246, 0.12);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 8px;
  font-size: 13px;
}
.tool-actions-title {
  font-weight: 600;
  color: var(--primary-color);
  margin-bottom: 8px;
  font-size: 13px;
}
.tool-action-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
  color: var(--text-color);
  border-bottom: 1px dashed rgba(0,0,0,0.04);
}
.tool-action-item:last-of-type {
  border-bottom: none;
}
.tool-action-name {
  font-weight: 500;
  white-space: nowrap;
  font-size: 13px;
}
.tool-action-detail {
  color: var(--text-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tool-actions-done {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid rgba(16, 185, 129, 0.15);
  font-size: 13px;
  font-weight: 600;
  color: #059669;
  text-align: center;
}

/* 文档返回面板 */
.download-panel {
  margin-top: 10px;
  background: linear-gradient(135deg, rgba(16, 185, 129, 0.05), rgba(59, 130, 246, 0.03));
  border: 1px solid rgba(16, 185, 129, 0.18);
  border-radius: 12px;
  padding: 12px 14px;
}
.download-panel-title {
  font-size: 13px;
  font-weight: 600;
  color: #059669;
  margin-bottom: 8px;
}
.download-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.6);
  border: 1px solid rgba(16, 185, 129, 0.12);
  border-radius: 10px;
  margin-bottom: 6px;
  transition: box-shadow 0.2s;
}
.download-item:hover {
  box-shadow: 0 2px 8px rgba(16, 185, 129, 0.15);
}
.download-item:last-child {
  margin-bottom: 0;
}
.download-file-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.download-file-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-color);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 侧栏 - 文档修改记录 */
.edited-docs-section {
  border-top: 1px solid var(--card-border);
  flex-shrink: 0;
  flex-grow: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  height: 168px;
}
.edited-docs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px 6px;
}
.edited-docs-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-color);
}
.edited-docs-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px 10px;
  scrollbar-width: thin;
  scrollbar-color: rgba(0,0,0,0.15) transparent;
}
.edited-docs-list::-webkit-scrollbar {
  width: 4px;
}
.edited-docs-list::-webkit-scrollbar-thumb {
  background: rgba(0,0,0,0.15);
  border-radius: 4px;
}
.edited-docs-list::-webkit-scrollbar-thumb:hover {
  background: rgba(0,0,0,0.25);
}
.edited-doc-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 8px;
  border-radius: 8px;
  transition: background 0.15s;
  margin-bottom: 2px;
}
.edited-doc-item:hover {
  background: rgba(255, 255, 255, 0.4);
}
.edited-doc-info {
  flex: 1;
  min-width: 0;
}
.edited-doc-name {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.4;
}
.edited-doc-time {
  display: block;
  font-size: 10px;
  color: var(--text-secondary);
  margin-top: 1px;
}
.edited-doc-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}

/* 输入区 */
.input-area {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 24px 16px;
  border-top: 1px solid rgba(255,255,255,0.3);
  background: rgba(255,255,255,0.1);
}
.input-row {
  display: flex;
  gap: 10px;
  align-items: center;
}
.file-row {
  flex-wrap: nowrap;
}
.file-select {
  flex: 1;
  min-width: 0;
}
.chat-upload-btn {
  flex-shrink: 0;
}

.chat-input { flex: 1; }
.send-btn { flex-shrink: 0; height: 40px; padding: 0 20px; }

@media (max-width: 767px) {
  .chat-main { height: calc(100vh - 140px); }
  .messages { padding: 14px; }
  .msg-content { max-width: 85%; }
  .cmd-groups { flex-direction: column; }
  .cmd-group { min-width: unset; width: 100%; }
  .input-area { padding: 12px 14px; }
}
</style>
