<template>
  <div>
    <div class="page-header">
      <h2>信息提取</h2>
      <p>查看文档提取结果、来源证据与多源融合数据</p>
    </div>

    <div class="card card-animate">
      <el-tabs v-model="tab">
        <!-- 按文件查看 -->
        <el-tab-pane label="按文件查看" name="byfile">
          <div v-for="f in pagedFileList" :key="f.file_id" class="file-block">
            <div class="file-block-header" @click="toggleFile(f)">
              <el-icon><Document /></el-icon>
              <span class="file-block-name">{{ f.file_name }}</span>
              <el-tag size="small" type="info">{{ f.field_count }} 个字段</el-tag>
              <el-icon style="margin-left: auto"><ArrowDown v-if="!f._open" /><ArrowUp v-else /></el-icon>
            </div>
            <div v-if="f._open" class="file-block-body">
              <div v-if="f._loading" class="loading-hint">加载中...</div>
              <template v-else-if="f._fields && f._fields.length">
                <div v-for="fd in getPagedFields(f)" :key="fd.field_name" class="field-row">
                  <span class="field-label">{{ fd.field_name }}</span>
                  <span class="field-value">{{ fd.field_value }}</span>
                  <span :class="['conf-badge', fd.confidence >= 0.8 ? 'high' : 'mid']"
                    >{{ (fd.confidence * 100).toFixed(0) }}%</span
                  >
                </div>
                <el-pagination
                  v-if="f._fields.length > 10"
                  style="margin-top: 8px; justify-content: flex-end"
                  layout="total, prev, pager, next"
                  :total="f._fields.length"
                  :page-size="10"
                  :current-page="filePages[String(f.file_id)] || 1"
                  size="small"
                  @current-change="p => setFilePage(f.file_id, p)"
                />
              </template>
              <div v-else class="loading-hint">暂无提取字段</div>
            </div>
          </div>
          <el-empty v-if="!fileList.length" description="暂无提取结果" :image-size="60" />
          <el-pagination
            v-if="fileList.length > 10"
            v-model:current-page="fileListPage"
            style="margin-top: 12px; justify-content: flex-end"
            layout="total, prev, pager, next"
            :total="fileList.length"
            :page-size="10"
            size="small"
          />
        </el-tab-pane>

        <!-- 提取结果 -->
        <el-tab-pane label="提取结果" name="raw">
          <div class="filter-bar">
            <el-select
              v-model="filterFileId"
              placeholder="按文件筛选"
              clearable
              size="small"
              style="width: 200px"
              @change="loadRaw"
            >
              <el-option v-for="f in fileList" :key="f.file_id" :label="f.file_name" :value="f.file_id" />
            </el-select>
            <el-input
              v-model="filterField"
              placeholder="按字段名搜索"
              clearable
              style="width: 180px"
              size="small"
              @clear="loadRaw"
              @keyup.enter="loadRaw"
            />
            <el-button size="small" type="primary" @click="loadRaw">搜索</el-button>
            <el-button v-if="rawSelection.length" type="danger" size="small" @click="batchDeleteRaw"
              >批量删除 ({{ rawSelection.length }})</el-button
            >
            <span class="total-hint">共 {{ rawTotal }} 条</span>
          </div>

          <el-table
            v-loading="loadingRaw"
            :data="rawResults"
            size="small"
            @selection-change="rows => (rawSelection = rows)"
          >
            <el-table-column type="selection" width="40" />
            <el-table-column prop="file_name" label="来源文件" width="160" show-overflow-tooltip />
            <el-table-column prop="field_name" label="字段名" width="120" show-overflow-tooltip />
            <el-table-column prop="field_value" label="字段值" show-overflow-tooltip min-width="200">
              <template #default="{ row }">
                <span v-if="editId !== row.id">{{ row.field_value }}</span>
                <el-input v-else v-model="editVal" size="small" @keyup.enter="saveEdit(row)" />
              </template>
            </el-table-column>
            <el-table-column prop="confidence" label="置信度" width="75">
              <template #default="{ row }">
                <span
                  :class="[
                    'conf-badge',
                    row.confidence >= 0.8 ? 'high' : row.confidence >= 0.5 ? 'mid' : 'low',
                  ]"
                >
                  {{ (row.confidence * 100).toFixed(0) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="extract_method" label="方法" width="80" />
            <el-table-column prop="source_position" label="来源位置" width="100" show-overflow-tooltip />
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button v-if="editId !== row.id" link type="primary" size="small" @click="startEdit(row)"
                  >修正</el-button
                >
                <template v-else>
                  <el-button link type="success" size="small" @click="saveEdit(row)">保存</el-button>
                  <el-button link size="small" @click="editId = null">取消</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>

          <el-pagination
            v-if="rawTotal > rawPageSize"
            v-model:current-page="rawPage"
            style="margin-top: 16px; justify-content: flex-end"
            layout="total, prev, pager, next"
            :total="rawTotal"
            :page-size="rawPageSize"
            size="small"
            @current-change="loadRaw"
          />
        </el-tab-pane>

        <!-- 融合结果 -->
        <el-tab-pane label="融合结果" name="fused">
          <div class="fused-meta">
            来自 {{ fusedMeta.source_files || 0 }} 个文件，融合得到 {{ fusedResults.length }} 个字段
          </div>
          <el-table v-loading="loadingFused" :data="fusedPagedResults" size="small">
            <el-table-column prop="field_name" label="字段名" width="130" />
            <el-table-column prop="field_value" label="融合值" show-overflow-tooltip min-width="200" />
            <el-table-column prop="confidence" label="置信度" width="75">
              <template #default="{ row }">
                <span :class="['conf-badge', row.confidence >= 0.8 ? 'high' : 'mid']"
                  >{{ (row.confidence * 100).toFixed(0) }}%</span
                >
              </template>
            </el-table-column>
            <el-table-column prop="source_file" label="最佳来源" width="150" show-overflow-tooltip />
            <el-table-column prop="candidates_count" label="候选数" width="70" />
            <el-table-column label="候选详情" width="80">
              <template #default="{ row }">
                <el-popover trigger="click" width="320">
                  <template #reference><el-button link size="small">查看</el-button></template>
                  <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px">
                    {{ row.field_name }} 的所有候选值
                  </div>
                  <div
                    v-for="(c, i) in row.all_candidates"
                    :key="i"
                    style="padding: 5px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.3)"
                  >
                    <div>
                      <b>{{ c.value }}</b>
                    </div>
                    <div style="font-size: 12px; color: var(--text-secondary)">
                      来源: {{ c.file }} | 权重: {{ c.weight }}
                    </div>
                  </div>
                </el-popover>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="fusedResults.length > 15"
            v-model:current-page="fusedPage"
            style="margin-top: 12px; justify-content: flex-end"
            layout="total, prev, pager, next"
            :total="fusedResults.length"
            :page-size="15"
            size="small"
          />
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { extractApi } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const tab = ref('byfile')
const rawResults = ref([])
const rawTotal = ref(0)
const rawPage = ref(1)
const rawPageSize = 15
const fusedResults = ref([])
const fusedMeta = ref({})
const fusedPage = ref(1)
const fusedPagedResults = computed(() => {
  const start = (fusedPage.value - 1) * 15
  return fusedResults.value.slice(start, start + 15)
})
const fileList = ref([])
const fileListPage = ref(1)
const pagedFileList = computed(() => {
  const start = (fileListPage.value - 1) * 10
  return fileList.value.slice(start, start + 10)
})
const filePages = ref({}) // file_id -> current page

const getPagedFields = f => {
  if (!f._fields) return []
  const page = filePages.value[String(f.file_id)] || 1
  const start = (page - 1) * 10
  return f._fields.slice(start, start + 10)
}
const setFilePage = (fileId, page) => {
  filePages.value[String(fileId)] = page
}
const loadingRaw = ref(false)
const loadingFused = ref(false)
const filterField = ref('')
const filterFileId = ref(null)
const editId = ref(null)
const editVal = ref('')
const rawSelection = ref([])

const loadRaw = async () => {
  loadingRaw.value = true
  try {
    const params = { page: rawPage.value, page_size: rawPageSize }
    if (filterField.value) params.field_name = filterField.value
    if (filterFileId.value) params.file_id = filterFileId.value
    const { data } = await extractApi.results(params)
    rawResults.value = data.results
    rawTotal.value = data.total
  } catch {
  } finally {
    loadingRaw.value = false
  }
}

const loadFiles = async () => {
  try {
    const { data } = await extractApi.files()
    fileList.value = data.files.map(f => ({ ...f, _open: false, _fields: null, _loading: false }))
  } catch {}
}

const toggleFile = async f => {
  const idx = fileList.value.indexOf(f)
  if (idx === -1) return
  // 用 Object.assign 触发响应式更新
  Object.assign(fileList.value[idx], { _open: !f._open })
  if (fileList.value[idx]._open && !fileList.value[idx]._fields) {
    Object.assign(fileList.value[idx], { _loading: true })
    try {
      const { data } = await extractApi.fileFields(f.file_id)
      Object.assign(fileList.value[idx], { _fields: data.fields, _loading: false })
    } catch {
      Object.assign(fileList.value[idx], { _fields: [], _loading: false })
    }
  }
}

const loadFused = async () => {
  loadingFused.value = true
  try {
    const { data } = await extractApi.fused()
    fusedResults.value = data.fused_results
    fusedMeta.value = { source_files: data.source_files }
  } catch {
  } finally {
    loadingFused.value = false
  }
}

const batchDeleteRaw = async () => {
  const count = rawSelection.value.length
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${count} 条提取结果？`, '批量删除', { type: 'warning' })
  } catch {
    return
  }
  try {
    await extractApi.batchDelete(rawSelection.value.map(r => r.id))
    ElMessage.success(`已删除 ${count} 条记录`)
    rawSelection.value = []
    loadRaw()
  } catch {
    ElMessage.error('删除失败')
  }
}

const startEdit = row => {
  editId.value = row.id
  editVal.value = row.field_value
}
const saveEdit = async row => {
  try {
    await extractApi.update(row.id, editVal.value)
    row.field_value = editVal.value
    row.confidence = 1
    row.extract_method = 'manual'
    editId.value = null
    ElMessage.success('已更新')
  } catch {
    ElMessage.error('更新失败')
  }
}

watch(tab, v => {
  if (v === 'fused') loadFused()
  if (v === 'byfile' && !fileList.value.length) loadFiles()
})
onMounted(() => {
  loadRaw()
  loadFiles()
})
</script>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.total-hint {
  margin-left: auto;
  font-size: 13px;
  color: var(--text-secondary);
}

.conf-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 6px;
}
.conf-badge.high {
  color: #3a9e7a;
  background: rgba(103, 197, 164, 0.15);
}
.conf-badge.mid {
  color: #b08a20;
  background: rgba(232, 184, 73, 0.15);
}
.conf-badge.low {
  color: #c05050;
  background: rgba(224, 112, 112, 0.15);
}

.fused-meta {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 12px;
}

.file-block {
  border: 1px solid rgba(255, 255, 255, 0.4);
  border-radius: 12px;
  margin-bottom: 10px;
  overflow: hidden;
  backdrop-filter: blur(8px);
}
.file-block-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  cursor: pointer;
  background: rgba(255, 255, 255, 0.2);
  font-size: 14px;
  font-weight: 500;
  color: var(--text-color);
  transition: background 0.2s;
}
.file-block-header:hover {
  background: rgba(255, 255, 255, 0.4);
}
.file-block-name {
  flex: 1;
}
.file-block-body {
  padding: 8px 16px;
}

.field-row {
  display: flex;
  align-items: center;
  padding: 7px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.2);
  font-size: 13px;
  color: var(--text-color);
}
.field-label {
  width: 120px;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.field-value {
  flex: 1;
}

.loading-hint {
  padding: 16px;
  color: var(--text-secondary);
  font-size: 13px;
  text-align: center;
}

@media (max-width: 767px) {
  .field-label {
    width: 80px;
    font-size: 12px;
  }
  .field-value {
    font-size: 12px;
  }
  .filter-bar {
    gap: 6px;
  }
  .total-hint {
    margin-left: 0;
  }
}
</style>
