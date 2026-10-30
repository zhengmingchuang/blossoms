<template>
  <div>
    <div class="page-header"><h2>文档管理</h2><p>批量上传、解析和管理多格式文档</p></div>

    <!-- 上传区 -->
    <div class="card card-animate" style="margin-bottom:20px;">
      <el-upload drag multiple :http-request="doUpload" :show-file-list="false" accept=".docx,.doc,.md,.xlsx,.xls,.txt">
        <div class="upload-inner">
          <div class="upload-icon-wrap">
            <el-icon :size="32" color="var(--primary-color)"><UploadFilled /></el-icon>
          </div>
          <p class="upload-text">拖拽或点击上传文档</p>
          <p class="upload-hint">支持 docx / xlsx / md / txt 格式，可批量上传</p>
        </div>
      </el-upload>
    </div>

    <!-- 文件列表 -->
    <div class="card card-animate" style="animation-delay:0.1s">
      <div class="list-header">
        <div class="list-header-left">
          <span class="list-title">文件列表 ({{ total }})</span>
          <el-popconfirm v-if="selected.length" :title="`确认删除选中的 ${selected.length} 个文件？`" @confirm="doBatchDel">
            <template #reference>
              <el-button size="small" type="danger" plain>批量删除 ({{ selected.length }})</el-button>
            </template>
          </el-popconfirm>
        </div>
        <div class="list-header-right">
          <el-select v-model="filterType" placeholder="类型筛选" size="small" clearable style="width:120px;" @change="load">
            <el-option label="docx" value="docx" />
            <el-option label="xlsx" value="xlsx" />
            <el-option label="md" value="md" />
            <el-option label="txt" value="txt" />
          </el-select>
          <el-button size="small" @click="load" :icon="Refresh">刷新</el-button>
        </div>
      </div>

      <el-table :data="files" v-loading="loading" @selection-change="onSelect" style="width:100%">
        <el-table-column type="selection" width="36" />
        <el-table-column prop="file_name" label="文件名" show-overflow-tooltip min-width="100" />
        <el-table-column prop="file_type" label="类型" width="70">
          <template #default="{ row }"><el-tag size="small">.{{ row.file_type }}</el-tag></template>
        </el-table-column>
        <el-table-column label="大小" width="85">
          <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="85">
          <template #default="{ row }"><el-tag size="small" :type="sType(row.parse_status)">{{ sLabel(row.parse_status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="片段" width="70">
          <template #default="{ row }">{{ row.chunk_count || 0 }}</template>
        </el-table-column>
        <el-table-column label="字段" width="60">
          <template #default="{ row }">{{ row.field_count || 0 }}</template>
        </el-table-column>
        <el-table-column label="用时" width="70">
          <template #default="{ row }">{{ row.parse_time ? row.parse_time + 's' : '-' }}</template>
        </el-table-column>
        <el-table-column prop="upload_time" label="上传时间" width="140">
          <template #default="{ row }">{{ fmtTime(row.upload_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="290" fixed="right">
          <template #default="{ row }">
            <div class="op-btns">
              <el-button link type="primary" size="small" @click="showDetail(row)">详情</el-button>
              <el-button link type="success" size="small" @click="doLlmExtract(row)" :loading="row._extracting" :disabled="row.parse_status !== 'done'">AI提取</el-button>
              <el-button link size="small" @click="doReparse(row)">重解析</el-button>
              <el-button link type="primary" size="small" :loading="row._downloading" @click="doDownload(row)">下载</el-button>
              <el-popconfirm title="确认删除？" @confirm="doDel(row)">
                <template #reference><el-button link type="danger" size="small">删除</el-button></template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-if="total > pageSize"
        style="margin-top:16px;justify-content:flex-end;"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="pageSize"
        v-model:current-page="page"
        @current-change="load"
        size="small"
      />
    </div>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVis" title="文档详情" width="750px" destroy-on-close>
      <template v-if="detail">
        <el-descriptions :column="3" border size="small" style="margin-bottom:16px;">
          <el-descriptions-item label="文件名">{{ detail.file.file_name }}</el-descriptions-item>
          <el-descriptions-item label="类型">.{{ detail.file.file_type }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ sLabel(detail.file.parse_status) }}</el-descriptions-item>
          <el-descriptions-item label="片段数">{{ detail.file.chunk_count }}</el-descriptions-item>
          <el-descriptions-item label="提取字段">{{ detail.file.field_count }}</el-descriptions-item>
          <el-descriptions-item label="解析用时">{{ detail.file.parse_time || 0 }}s</el-descriptions-item>
        </el-descriptions>
        <el-tabs>
          <el-tab-pane label="文档内容">
            <div v-for="ch in detail.chunks" :key="ch.id" class="chunk-item">
              <el-tag size="small" :type="ch.chunk_type === 'title' ? '' : 'info'" style="margin-right:8px;flex-shrink:0;">{{ ch.chunk_type }}</el-tag>
              <span class="chunk-pos">[{{ ch.source_position }}]</span>
              <span>{{ ch.chunk_text }}</span>
            </div>
            <el-empty v-if="!detail.chunks.length" description="暂无内容" :image-size="50" />
          </el-tab-pane>
          <el-tab-pane :label="`提取结果 (${detail.extractions.length})`">
            <el-table :data="detail.extractions" size="small">
              <el-table-column prop="field_name" label="字段" width="120" />
              <el-table-column prop="field_value" label="值" show-overflow-tooltip />
              <el-table-column prop="confidence" label="置信度" width="75">
                <template #default="{ row }">{{ (row.confidence * 100).toFixed(0) }}%</template>
              </el-table-column>
              <el-table-column prop="extract_method" label="方法" width="80" />
              <el-table-column prop="source_position" label="来源位置" width="100" show-overflow-tooltip />
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { docApi } from '../api'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'

const fmtTime = (t) => t ? String(t).replace(/\.\d+$/, '').slice(0, 19) : '-'

const files = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 5
const loading = ref(false)
const filterType = ref('')
const detailVis = ref(false)
const detail = ref(null)
const selected = ref([])
let pollTimer = null

const hasPending = () => files.value.some(f => f.parse_status === 'pending' || f.parse_status === 'parsing')

const startPolling = () => {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const { data } = await docApi.list(page.value, pageSize)
      files.value = data.files
      total.value = data.total
      if (!hasPending()) { stopPolling(); ElMessage.success('所有文档解析完成') }
    } catch {}
  }, 3000)
}

const stopPolling = () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }

const load = async () => {
  loading.value = true
  try {
    const { data } = await docApi.list(page.value, pageSize)
    files.value = data.files; total.value = data.total
    if (hasPending()) startPolling()
  } catch {} finally { loading.value = false }
}

const doUpload = async ({ file }) => {
  const fd = new FormData(); fd.append('files', file)
  try { await docApi.upload(fd); ElMessage.success('上传成功，正在解析...'); await load() }
  catch { ElMessage.error('上传失败') }
}

const onSelect = rows => { selected.value = rows.map(r => r.id) }

const doBatchDel = async () => {
  try {
    const { data } = await docApi.batchDelete(selected.value)
    ElMessage.success(data.message); selected.value = []; load()
  } catch { ElMessage.error('批量删除失败') }
}

const showDetail = async row => {
  try { detail.value = (await docApi.detail(row.id)).data; detailVis.value = true }
  catch { ElMessage.error('获取失败') }
}

const doReparse = async row => {
  try { await docApi.reparse(row.id); ElMessage.success('已提交重新解析'); await load() } catch {}
}

const doLlmExtract = async row => {
  row._extracting = true
  try {
    const { data } = await docApi.extract(row.id)
    ElMessage.success(`AI提取完成：新增${data.new_fields || 0}个字段，共${data.total_fields || 0}个`)
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || 'AI提取失败，请检查LLM配置')
  } finally { row._extracting = false }
}

const doDel = async row => { try { await docApi.delete(row.id); ElMessage.success('已删除'); load() } catch {} }

const doDownload = async row => {
  row._downloading = true
  try {
    const res = await docApi.download(row.id)
    const blob = new Blob([res.data])
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.file_name
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('下载已开始')
  } catch (e) {
    let msg = '下载失败'
    const d = e?.response?.data
    if (d instanceof Blob) {
      try { const j = JSON.parse(await d.text()); if (j?.detail) msg = j.detail } catch {}
    } else if (d?.detail) {
      msg = d.detail
    }
    ElMessage.error(msg)
  } finally {
    row._downloading = false
  }
}
const formatSize = b => b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(1) + ' KB' : (b / 1048576).toFixed(1) + ' MB'
const sType = s => ({ done: 'success', error: 'danger', parsing: 'warning', pending: 'info' }[s] || 'info')
const sLabel = s => ({ done: '完成', error: '失败', parsing: '解析中', pending: '等待' }[s] || s)

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 10px;
}
.list-header-left { display: flex; align-items: center; gap: 12px; }
.list-header-right { display: flex; gap: 8px; align-items: center; }
.list-title { font-weight: 600; color: var(--text-color); font-size: 15px; }

.upload-inner { padding: 28px 0; text-align: center; }
.upload-icon-wrap {
  width: 64px; height: 64px;
  border-radius: 16px;
  background: rgba(255,255,255,0.5);
  border: 1px solid rgba(255,255,255,0.6);
  display: flex; align-items: center; justify-content: center;
  margin: 0 auto 12px;
  transition: transform 0.2s;
}
.el-upload-dragger:hover .upload-icon-wrap { transform: scale(1.05); }
.upload-text { font-size: 15px; color: var(--text-color); margin-top: 4px; font-weight: 500; }
.upload-hint { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }

.chunk-item {
  padding: 10px 0;
  border-bottom: 1px solid rgba(255,255,255,0.3);
  font-size: 13px;
  line-height: 1.6;
  display: flex;
  align-items: flex-start;
  gap: 4px;
  color: var(--text-color);
}
.chunk-pos { color: var(--text-secondary); font-size: 12px; flex-shrink: 0; }

.op-btns {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-wrap: nowrap;
  white-space: nowrap;
}

@media (max-width: 767px) {
  .upload-inner { padding: 16px 0; }
  .upload-text { font-size: 13px; }
}
</style>
