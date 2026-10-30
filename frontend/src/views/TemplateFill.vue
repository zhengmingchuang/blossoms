<template>
  <div>
    <div class="page-header"><h2>模板填报</h2><p>上传模板表格，系统自动从已上传文档中匹配数据并填写</p></div>

    <div class="tpl-grid">
      <!-- 左上：上传模板 + 数据源 -->
      <div class="cell cell-tl">
        <!-- 上传模板 -->
        <div class="card card-animate">
          <h3 class="sec-title">上传模板</h3>
          <el-upload drag :http-request="doUpload" :show-file-list="false" accept=".docx,.doc,.xlsx,.xls">
            <div class="upload-inner">
              <div class="upload-icon-wrap">
                <el-icon :size="28" color="var(--primary-color)"><UploadFilled /></el-icon>
              </div>
              <p class="upload-text">拖拽或点击上传 Word/Excel 模板</p>
            </div>
          </el-upload>
          <div style="margin-top:12px;">
            <el-input v-model="userReq" type="textarea" :rows="3" placeholder="输入用户要求（如：智能填表、从2020/7/1到2020/8/31的数据填入模板、城市：德州市 等）" />
          </div>
          <div class="req-upload-actions">
            <el-upload ref="reqUploadRef" class="req-upload-txt" :http-request="uploadReqFile" :show-file-list="true" accept=".txt" :limit="1" :on-exceed="() => ElMessage.warning('只能上传一个要求文件')">
              <el-button size="small" plain>或上传用户要求 txt 文件</el-button>
            </el-upload>
            <el-button
              v-if="uploadResult"
              type="primary"
              class="upload-fill-btn"
              :disabled="!canFillFromUpload"
              :loading="filling === uploadResult.template_id"
              @click="fillFromUpload"
            >填写</el-button>
          </div>
          <div v-if="uploadResult" class="upload-result">
            <div class="upload-result-text">
              ✅ 识别到 <b>{{ uploadResult.field_count }}</b> 个字段，模式:
              <el-tag size="small" :type="uploadResult.mode === 'table_fill' ? 'warning' : 'info'">
                {{ uploadResult.mode === 'table_fill' ? '表格批量填充' : '字段匹配填充' }}
              </el-tag>
              <el-tag v-if="uploadResult.multi_sheet" size="small" type="success" style="margin-left:4px;">
                多表单 ({{ uploadResult.sheet_count }}个)
              </el-tag>
            </div>
            <div v-if="uploadResult.sheets && uploadResult.sheets.length" class="upload-sheets">
              <div v-for="(s, si) in uploadResult.sheets" :key="si" class="sheet-info">
                <span class="sheet-name">📄 {{ s.sheet }}</span>
                <el-tag v-for="c in s.columns" :key="c" size="small" style="margin:2px;">{{ c }}</el-tag>
              </div>
            </div>
            <div v-else-if="uploadResult.columns && uploadResult.columns.length" class="upload-cols">
              <el-tag v-for="c in uploadResult.columns" :key="c" size="small">{{ c }}</el-tag>
            </div>
          </div>
        </div>

        <!-- 数据源（同「文档管理」） -->
        <div class="card card-animate" style="animation-delay:0.05s">
          <div class="sec-header">
            <h3 class="sec-title" style="margin-bottom:0;">数据源</h3>
            <el-button link type="primary" size="small" @click="goDocuments">前往文档管理 →</el-button>
          </div>
          <p class="sec-desc">在此上传的文件会进入「文档管理」并自动解析，解析完成后即可在「填写」时作为数据源使用。</p>
          <el-upload
            drag
            multiple
            :http-request="doUploadSource"
            :show-file-list="false"
            accept=".docx,.doc,.md,.xlsx,.xls,.txt"
          >
            <div class="upload-inner src-upload-inner">
              <div class="upload-icon-wrap">
                <el-icon :size="24" color="var(--primary-color)"><UploadFilled /></el-icon>
              </div>
              <p class="upload-text">拖拽或点击上传数据源文件</p>
              <p class="upload-hint">支持 docx / xlsx / md / txt 格式，可批量上传</p>
            </div>
          </el-upload>
          <div v-if="recentSources.length" class="recent-sources">
            <div class="recent-title">最近上传：</div>
            <div class="recent-list">
              <el-tag
                v-for="f in recentSources"
                :key="f.id"
                size="small"
                :type="f.parse_status === 'done' ? 'success' : f.parse_status === 'error' ? 'danger' : 'warning'"
                style="margin:2px;"
              >
                {{ f.file_name }}
                <span style="margin-left:4px;opacity:0.75;">({{ f.parse_status === 'done' ? '已解析' : f.parse_status === 'error' ? '失败' : '解析中' }})</span>
              </el-tag>
            </div>
          </div>
        </div>

      </div>

      <!-- 右上：填写结果 -->
      <div class="cell cell-tr">
        <!-- 填写结果 -->
        <div class="card card-animate stretch-card" style="animation-delay:0.15s" v-if="fillResult">
          <div class="result-header">
            <h3 class="sec-title" style="margin-bottom:0;">填写结果</h3>
            <div style="display:flex;gap:8px;">
              <el-button size="small" @click="previewVis = true">预览详情</el-button>
              <el-button type="primary" size="small" @click="download">导出文件</el-button>
            </div>
          </div>
          <div class="mini-stats">
            <div class="mini-stat">
              <div class="mini-num">{{ fillResult.field_count }}</div>
              <div class="mini-label">总字段</div>
            </div>
            <div class="mini-stat">
              <div class="mini-num" style="color:var(--primary-color);">{{ fillResult.filled_count }}</div>
              <div class="mini-label">已填写</div>
            </div>
            <div class="mini-stat">
              <div class="mini-num" style="color:#3a9e7a;">{{ (fillResult.accuracy * 100).toFixed(0) }}%</div>
              <div class="mini-label">填写率</div>
            </div>
            <div class="mini-stat">
              <div class="mini-num">{{ fillResult.elapsed_seconds }}s</div>
              <div class="mini-label">用时</div>
            </div>
            <div class="mini-stat" v-if="fillResult.total_rows">
              <div class="mini-num">{{ fillResult.total_rows }}</div>
              <div class="mini-label">数据行</div>
            </div>
          </div>
          <div v-if="fillResult.source_file" class="source-hint">数据来源: {{ fillResult.source_file }}</div>
          <div class="result-table-wrap">
            <el-table :data="fillResult.details" size="small" height="100%">
              <el-table-column prop="field" label="字段/列名" width="140" show-overflow-tooltip />
              <el-table-column prop="value" label="填写值" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="!row.value" style="color:var(--text-secondary);font-style:italic;">未匹配</span>
                  <span v-else>{{ row.value }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="confidence" width="100">
                <template #header>
                  <span style="display:inline-flex;align-items:center;gap:2px;">
                    置信度
                    <el-tooltip placement="top" effect="light" :show-after="100">
                      <template #content>
                        <div style="max-width:340px;font-size:12px;line-height:1.7;">
                          <div style="font-weight:600;margin-bottom:4px;">置信度（0–100%）多维度加权计算</div>
                          <div><b>表格匹配模式</b>（权重）：</div>
                          <div>· 列名匹配度 × 25%（精确=1.0 / 包含=0.9 / 模糊=0.75）</div>
                          <div>· 数据源匹配分 × 25%（默认 0.85）</div>
                          <div>· 数据类型一致性 × 20%（数值列检查实际是否数值）</div>
                          <div>· 非空填充率 × 15%</div>
                          <div>· 同列格式一致性 × 10%</div>
                          <div>· 取值合理性 × 5%（含异常长度检测）</div>
                          <div style="margin-top:6px;"><b>LLM 提取模式</b>（权重）：</div>
                          <div>· LLM 基础分 0.95 × 20%</div>
                          <div>· 非空填充率 × 20% · 类型一致性 × 20%</div>
                          <div>· 同列格式一致性 × 25% · 取值合理性 × 15%</div>
                          <div>· 多批采样不一致时再乘 0.70~1.00 的惩罚因子</div>
                          <div style="margin-top:6px;color:#3a9e7a;">≥ 80% 高 · 60–79% 中 · &lt; 60% 低</div>
                        </div>
                      </template>
                      <el-icon style="font-size:13px;color:var(--text-secondary);cursor:help;"><InfoFilled /></el-icon>
                    </el-tooltip>
                  </span>
                </template>
                <template #default="{ row }">
                  <span :class="['conf-badge', row.confidence >= 0.8 ? 'high' : row.confidence >= 0.6 ? 'mid' : 'low']">
                    {{ (row.confidence * 100).toFixed(0) }}%
                  </span>
                </template>
              </el-table-column>
              <el-table-column prop="source_file" label="来源" width="120" show-overflow-tooltip />
            </el-table>
          </div>
        </div>
        <div class="card card-animate stretch-card stretch-empty" style="animation-delay:0.15s" v-else>
          <el-empty description="上传模板识别后点「填写」，或在模板列表中点「自动填写」查看结果" :image-size="80" />
        </div>
      </div>

      <!-- 左下：模板列表 -->
      <div class="cell cell-bl">
        <div class="card card-animate stretch-card" style="animation-delay:0.1s">
          <div class="sec-header">
            <h3 class="sec-title" style="margin-bottom:0;">模板列表</h3>
            <el-button v-if="tplSelection.length" type="danger" size="small" @click="batchDeleteTpl">批量删除 ({{ tplSelection.length }})</el-button>
          </div>
          <el-table :data="tplPagedList" size="small" @selection-change="rows => tplSelection = rows">
            <el-table-column type="selection" width="40" />
            <el-table-column prop="template_name" label="名称" show-overflow-tooltip min-width="140" />
            <el-table-column prop="template_type" label="类型" width="55" />
            <el-table-column label="模式" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="row.template_mode === 'table_fill' ? 'warning' : 'info'">
                  {{ row.template_mode === 'table_fill' ? '表格' : '字段' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="field_count" label="字段" width="50" />
            <el-table-column label="操作" width="200">
              <template #default="{ row }">
                <el-button link type="primary" size="small" :loading="filling === row.id" @click="doFill(row)">自动填写</el-button>
                <el-button link size="small" @click="showReq(row)">要求</el-button>
                <el-button link size="small" @click="showMappings(row)">映射</el-button>
                <el-popconfirm title="删除？" @confirm="doDelete(row)">
                  <template #reference><el-button link type="danger" size="small">删除</el-button></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!templates.length" description="暂无模板" :image-size="50" />
          <el-pagination
            v-if="templates.length > tplPageSize"
            style="margin-top:12px;justify-content:flex-end;"
            layout="total, prev, pager, next"
            :total="templates.length"
            :page-size="tplPageSize"
            v-model:current-page="tplPage"
            size="small"
          />
        </div>
      </div>

      <!-- 右下：填写历史 -->
      <div class="cell cell-br">
        <div class="card card-animate stretch-card" style="animation-delay:0.22s;">
          <div class="history-header">
            <h3 class="sec-title" style="margin-bottom:0;">填写历史</h3>
            <div style="display:flex;gap:8px;align-items:center;">
              <el-button v-if="historySelection.length" type="danger" size="small" @click="batchDeleteHistory">批量删除 ({{ historySelection.length }})</el-button>
              <el-input v-model="historyKeyword" placeholder="搜索模板名..." size="small" clearable style="width:180px;" @clear="loadHistory" @keyup.enter="loadHistory">
                <template #append><el-button @click="loadHistory" size="small">搜索</el-button></template>
              </el-input>
            </div>
          </div>
          <el-table :data="historyList" size="small" v-if="historyList.length" max-height="260" @selection-change="rows => historySelection = rows">
            <el-table-column type="selection" width="40" />
            <el-table-column prop="task_name" label="模板" show-overflow-tooltip min-width="140">
              <template #default="{ row }">{{ row.task_name.replace('填写: ', '') }}</template>
            </el-table-column>
            <el-table-column prop="accuracy" label="填写率" width="70">
              <template #default="{ row }"><span style="color:#3a9e7a;font-weight:600;">{{ (row.accuracy * 100).toFixed(0) }}%</span></template>
            </el-table-column>
            <el-table-column prop="fill_time" label="时间" width="150" show-overflow-tooltip />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button link type="primary" size="small" :disabled="!row.file_exists" @click="downloadHistory(row)">下载</el-button>
                <el-popconfirm title="确认删除该记录？" @confirm="deleteHistory(row)">
                  <template #reference>
                    <el-button link type="danger" size="small">删除</el-button>
                  </template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无填写记录" :image-size="40" />
          <el-pagination v-if="historyTotal > 5" style="margin-top:10px;justify-content:flex-end;" layout="total, prev, pager, next" :total="historyTotal" :page-size="5" v-model:current-page="historyPage" @current-change="loadHistory" size="small" />
        </div>
      </div>
    </div>

    <!-- 用户要求编辑弹窗 -->
    <el-dialog v-model="reqVis" title="编辑用户要求" width="500px" destroy-on-close>
      <el-input v-model="editReq" type="textarea" :rows="5" placeholder="输入用户要求..." />
      <template #footer>
        <el-button @click="reqVis = false">取消</el-button>
        <el-button type="primary" @click="saveReq">保存</el-button>
      </template>
    </el-dialog>

    <!-- 映射详情弹窗 -->
    <el-dialog v-model="mappingVis" title="字段映射详情" width="650px" destroy-on-close>
      <el-table :data="mappings" size="small">
        <el-table-column prop="field_name" label="模板字段" width="130" show-overflow-tooltip />
        <el-table-column prop="matched_value" label="匹配值" show-overflow-tooltip />
        <el-table-column prop="confidence" label="置信度" width="75">
          <template #default="{ row }">
            <span :class="['conf-badge', row.confidence >= 0.8 ? 'high' : 'mid']">{{ (row.confidence * 100).toFixed(0) }}%</span>
          </template>
        </el-table-column>
        <el-table-column prop="source_file" label="来源" width="120" show-overflow-tooltip />
      </el-table>
    </el-dialog>

    <!-- 预览弹窗 -->
    <el-dialog v-model="previewVis" title="填写结果预览" width="700px" destroy-on-close>
      <div v-if="fillResult" style="max-height:500px;overflow-y:auto;">
        <div v-for="d in fillResult.details" :key="d.field" class="preview-row">
          <div class="preview-label">{{ d.field }}</div>
          <div class="preview-value" :class="{ empty: !d.value }">{{ d.value || '（未匹配）' }}</div>
          <div class="preview-meta">
            <span :class="['conf-badge', d.confidence >= 0.8 ? 'high' : 'mid']">{{ (d.confidence * 100).toFixed(0) }}%</span>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="previewVis = false">关闭</el-button>
        <el-button type="primary" @click="download(); previewVis = false">导出文件</el-button>
      </template>
    </el-dialog>

    <!-- 数据源选择弹窗 -->
    <el-dialog v-model="sourceVis" title="选择数据源" width="600px" destroy-on-close>
      <p style="margin-bottom:12px;font-size:13px;color:var(--text-secondary);">请勾选要用于填充的数据源文件</p>
      <el-checkbox v-model="sourceSelectAll" :indeterminate="sourceIndeterminate" @change="onSourceSelectAll" style="margin-bottom:8px;">全选</el-checkbox>
      <el-checkbox-group v-model="selectedSourceIds" style="display:flex;flex-direction:column;gap:6px;">
        <el-checkbox v-for="f in sourceFiles" :key="f.id" :value="f.id" style="margin-left:0;">
          <span style="font-size:13px;">{{ f.file_name }}</span>
          <el-tag size="small" style="margin-left:6px;">{{ f.file_type }}</el-tag>
          <span style="font-size:12px;color:var(--text-secondary);margin-left:6px;">{{ f.field_count }}字段 · {{ f.chunk_count }}片段</span>
        </el-checkbox>
      </el-checkbox-group>
      <el-empty v-if="!sourceFiles.length" description="暂无已解析的文档，请先在文档管理中上传" :image-size="50" />
      <template #footer>
        <el-button @click="onSourceCancel">取消</el-button>
        <el-button type="primary" @click="confirmFill" :loading="!!filling" :disabled="selectedSourceIds.length === 0">开始填写</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { tplApi, docApi } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, InfoFilled } from '@element-plus/icons-vue'

const router = useRouter()
const goDocuments = () => router.push({ name: 'Documents' })

const templates = ref([])
const tplPage = ref(1)
const tplPageSize = 5
const tplPagedList = computed(() => {
  const start = (tplPage.value - 1) * tplPageSize
  return templates.value.slice(start, start + tplPageSize)
})
const uploadResult = ref(null)
const fillResult = ref(null)
const filling = ref(null)
const userReq = ref('')
const mappingVis = ref(false)
const mappings = ref([])
const previewVis = ref(false)
const reqVis = ref(false)
const editReq = ref('')
const editTid = ref(null)
const reqFile = ref(null)
const reqUploadRef = ref(null)
const uploadHadRequirement = ref(false)
/** 本次填写是否从「上传模板」卡片的「填写」进入（成功后需重置上传区） */
const pendingFromUpload = ref(false)

const canFillFromUpload = computed(() =>
  uploadHadRequirement.value || !!(userReq.value && userReq.value.trim()) || !!reqFile.value
)

const sourceVis = ref(false)
const sourceFiles = ref([])
const selectedSourceIds = ref([])
const pendingFillRow = ref(null)
const sourceSelectAll = computed({
  get: () => sourceFiles.value.length > 0 && selectedSourceIds.value.length === sourceFiles.value.length,
  set: () => {}
})
const sourceIndeterminate = computed(() => selectedSourceIds.value.length > 0 && selectedSourceIds.value.length < sourceFiles.value.length)
const onSourceSelectAll = (val) => { selectedSourceIds.value = val ? sourceFiles.value.map(f => f.id) : [] }

const tplSelection = ref([])
const historySelection = ref([])

const srcUploading = ref(false)
const recentSources = ref([])
let srcPollTimer = null

const stopSrcPolling = () => { if (srcPollTimer) { clearInterval(srcPollTimer); srcPollTimer = null } }

const refreshRecentSources = async () => {
  if (!recentSources.value.length) return
  try {
    const { data } = await docApi.list(1, 50)
    const map = new Map((data.files || []).map(f => [f.id, f]))
    recentSources.value = recentSources.value.map(r => map.get(r.id) || r)
    const stillPending = recentSources.value.some(f => f.parse_status === 'pending' || f.parse_status === 'parsing')
    if (!stillPending) {
      stopSrcPolling()
      ElMessage.success('数据源解析完成，可在「填写」时选择使用')
    }
  } catch {}
}

const startSrcPolling = () => {
  stopSrcPolling()
  srcPollTimer = setInterval(refreshRecentSources, 3000)
}

const doUploadSource = async ({ file }) => {
  srcUploading.value = true
  try {
    const fd = new FormData()
    fd.append('files', file)
    const { data } = await docApi.upload(fd)
    const uploaded = (data.files || data.uploaded || []).map(f => ({
      id: f.id ?? f.file_id,
      file_name: f.file_name ?? file.name,
      parse_status: f.parse_status || 'pending',
    })).filter(f => f.id != null)
    if (uploaded.length) {
      const ids = new Set(recentSources.value.map(r => r.id))
      recentSources.value = [...uploaded.filter(f => !ids.has(f.id)), ...recentSources.value].slice(0, 8)
      startSrcPolling()
    }
    ElMessage.success(`数据源 ${file.name} 已上传，正在解析...`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '上传失败')
  } finally {
    srcUploading.value = false
  }
}

const loadList = async () => { try { templates.value = (await tplApi.list()).data.templates } catch {} }
const uploadReqFile = ({ file }) => { reqFile.value = file }

const readTxtFileAsText = (file) =>
  new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result ?? ''))
    r.onerror = () => reject(r.error)
    r.readAsText(file, 'UTF-8')
  })

/** 与后端一致：文本框 + txt 文件内容用空行拼接；仅发合并后的字符串，减少 multipart 丢字段问题 */
const mergeUserRequirement = async (text, txtFile) => {
  const t = (text || '').trim()
  if (!txtFile) return t
  let fileText = ''
  try {
    fileText = (await readTxtFileAsText(txtFile)).trim()
  } catch {
    fileText = ''
  }
  if (!t) return fileText
  if (!fileText) return t
  return `${t}\n\n${fileText}`
}

const doUpload = async ({ file }) => {
  const mergedReq = await mergeUserRequirement(userReq.value, reqFile.value)
  const hadReq = !!mergedReq.trim()
  const fd = new FormData()
  fd.append('file', file)
  fd.append('user_requirement', mergedReq)
  try {
    const { data } = await tplApi.upload(fd)
    uploadResult.value = data
    uploadHadRequirement.value = hadReq
    const tid = data.template_id
    if (tid != null && hadReq) {
      try {
        await tplApi.updateRequirement(tid, mergedReq.trim())
      } catch {
        ElMessage.warning('模板已上传，但用户要求未写入成功，请点击「要求」手动保存')
      }
    }
    userReq.value = mergedReq.trim()
    ElMessage.success('模板上传成功')
    reqFile.value = null
    await loadList()
  } catch { ElMessage.error('上传失败') }
}

const fillFromUpload = async () => {
  const tid = uploadResult.value?.template_id
  if (tid == null) return
  if (userReq.value.trim()) {
    try {
      await tplApi.updateRequirement(tid, userReq.value)
    } catch {
      ElMessage.error('更新要求失败')
      return
    }
  }
  await doFill({ id: tid }, { fromUpload: true })
}

const doFill = async (row, { fromUpload = false } = {}) => {
  pendingFillRow.value = row
  pendingFromUpload.value = fromUpload
  try {
    const { data } = await docApi.list(1, 100)
    sourceFiles.value = (data.files || []).filter(f => f.parse_status === 'done')
    selectedSourceIds.value = []; sourceVis.value = true
  } catch {
    pendingFromUpload.value = false
    ElMessage.error('获取文档列表失败')
  }
}

const onSourceCancel = () => {
  sourceVis.value = false
  pendingFromUpload.value = false
  pendingFillRow.value = null
}

const resetUploadPanel = () => {
  uploadResult.value = null
  uploadHadRequirement.value = false
  reqFile.value = null
  userReq.value = ''
  reqUploadRef.value?.clearFiles()
}

const confirmFill = async () => {
  const row = pendingFillRow.value
  if (!row) return
  const fromUploadCard = pendingFromUpload.value
  sourceVis.value = false; filling.value = row.id
  try {
    const { data } = await tplApi.fill(row.id, selectedSourceIds.value)
    fillResult.value = data
    ElMessage.success(`填写完成：${data.filled_count}/${data.field_count}`)
    loadHistory()
    await loadList()
    if (fromUploadCard) {
      resetUploadPanel()
      pendingFromUpload.value = false
    }
  } catch (e) { ElMessage.error('填写失败：' + (e.response?.data?.detail || '请检查数据')) }
  finally { filling.value = null }
}

const showReq = row => { editTid.value = row.id; editReq.value = row.user_requirement || ''; reqVis.value = true }
const saveReq = async () => {
  try {
    await tplApi.updateRequirement(editTid.value, editReq.value)
    ElMessage.success('已保存')
    reqVis.value = false
    if (uploadResult.value?.template_id === editTid.value) {
      userReq.value = editReq.value
    }
    await loadList()
  } catch { ElMessage.error('保存失败') }
}
const showMappings = async row => {
  try { mappings.value = (await tplApi.mappings(row.id)).data.mappings; mappingVis.value = true }
  catch { ElMessage.error('获取映射失败') }
}
const doDelete = async row => { try { await tplApi.delete(row.id); ElMessage.success('已删除'); loadList() } catch {} }

const batchDeleteTpl = async () => {
  const count = tplSelection.value.length
  try { await ElMessageBox.confirm(`确定删除选中的 ${count} 个模板？`, '批量删除', { type: 'warning' }) } catch { return }
  try {
    await tplApi.batchDelete(tplSelection.value.map(r => r.id))
    ElMessage.success(`已删除 ${count} 个模板`)
    tplSelection.value = []
    loadList()
  } catch { ElMessage.error('删除失败') }
}

const batchDeleteHistory = async () => {
  const count = historySelection.value.length
  try { await ElMessageBox.confirm(`确定删除选中的 ${count} 条记录？`, '批量删除', { type: 'warning' }) } catch { return }
  try {
    await tplApi.batchDeleteHistory(historySelection.value.map(r => r.id))
    ElMessage.success(`已删除 ${count} 条记录`)
    historySelection.value = []
    loadHistory()
  } catch { ElMessage.error('删除失败') }
}
const downloadErr = async (e) => {
  const st = e.response?.status
  const d = e.response?.data
  let msg = '下载失败'
  if (st === 404) msg = '文件不存在，可能已被清理，请重新填写后导出'
  else if (st === 403) msg = '无权下载该文件'
  else if (typeof d === 'string' && d) msg = d
  else if (d?.detail) msg = typeof d.detail === 'string' ? d.detail : msg
  else if (d instanceof Blob) {
    try {
      const text = await d.text()
      const j = JSON.parse(text)
      if (typeof j.detail === 'string') msg = j.detail
    } catch { /* keep msg */ }
  } else if (st === 400) msg = '请求无效'
  ElMessage.error(msg)
}
/** 将模板下载接口的成功响应落盘；若服务端误返回 JSON，给出可读错误 */
const saveTplDownload = async (res, saveAsName) => {
  const ct = (res.headers['content-type'] || '').toLowerCase()
  if (ct.includes('application/json') && res.data instanceof Blob) {
    const text = await res.data.text()
    try {
      const j = JSON.parse(text)
      const err = new Error(typeof j.detail === 'string' ? j.detail : '下载失败')
      err._tplDl = { status: res.status, data: j }
      throw err
    } catch (e) {
      if (e._tplDl) throw e
    }
  }
  const blob = new Blob([res.data])
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = saveAsName
  a.click()
  URL.revokeObjectURL(url)
}
const hasTaskId = (r) =>
  r?.task_id != null && r.task_id !== '' && Number.isFinite(Number(r.task_id))

const download = async () => {
  if (!fillResult.value?.output_file) return
  const name = fillResult.value.output_file
  const tid = hasTaskId(fillResult.value) ? Number(fillResult.value.task_id) : null
  try {
    const res = tid != null
      ? await tplApi.downloadByTask(tid)
      : await tplApi.download(name)
    await saveTplDownload(res, name)
  } catch (e) {
    if (tid != null && !e._tplDl && e.response?.status === 404) {
      try {
        const res = await tplApi.download(name)
        await saveTplDownload(res, name)
        return
      } catch (e2) {
        await downloadErr(e2)
        return
      }
    }
    if (e._tplDl) {
      ElMessage.error(e.message || '下载失败')
    } else {
      await downloadErr(e)
    }
  }
}

const historyList = ref([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyKeyword = ref('')
const loadHistory = async () => {
  try {
    const { data } = await tplApi.history({ page: historyPage.value, page_size: 5, keyword: historyKeyword.value })
    historyList.value = data.history; historyTotal.value = data.total
  } catch {}
}
const downloadHistory = async (row) => {
  if (row.id == null) return
  const name = row.output_file || `export-${row.id}.xlsx`
  try {
    const res = await tplApi.downloadByTask(row.id)
    await saveTplDownload(res, name)
  } catch (e) {
    if (e._tplDl) {
      ElMessage.error(e.message || '下载失败')
    } else {
      await downloadErr(e)
    }
  }
}

const deleteHistory = async (row) => {
  try {
    await tplApi.deleteHistory(row.id)
    ElMessage.success('已删除')
    loadHistory()
  } catch (e) { ElMessage.error(e.response?.data?.detail || '删除失败') }
}

onMounted(() => { loadList(); loadHistory() })
onBeforeUnmount(stopSrcPolling)
</script>

<style scoped>
.tpl-grid {
  display: grid;
  grid-template-columns: 5fr 7fr;
  grid-template-rows: auto auto;
  gap: 20px;
  align-items: stretch;
}
.cell { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
.cell-tl { grid-column: 1; grid-row: 1; }
.cell-tr { grid-column: 2; grid-row: 1; }
.cell-bl { grid-column: 1; grid-row: 2; }
.cell-br { grid-column: 2; grid-row: 2; }
.stretch-card { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.stretch-empty { align-items: center; justify-content: center; }
.stretch-empty :deep(.el-empty) { width: 100%; }

.sec-title { font-size: 15px; font-weight: 600; color: var(--text-color); margin-bottom: 14px; transition: color 0.5s; }
.sec-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.sec-desc { font-size: 12px; color: var(--text-secondary); margin: -6px 0 12px; line-height: 1.6; }

.src-upload-inner { padding: 14px 0; }
.src-upload-inner .upload-icon-wrap { width: 48px; height: 48px; border-radius: 12px; }
.src-upload-inner .upload-text { font-size: 13px; }
.upload-hint { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.recent-sources { margin-top: 12px; }
.recent-title { font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.recent-list { display: flex; flex-wrap: wrap; gap: 4px; }

.upload-inner { padding: 20px 0; text-align: center; }
.upload-icon-wrap {
  width: 56px; height: 56px;
  border-radius: 14px;
  background: rgba(255,255,255,0.5);
  border: 1px solid rgba(255,255,255,0.6);
  display: flex; align-items: center; justify-content: center;
  margin: 0 auto 10px;
  transition: transform 0.2s;
}
.el-upload-dragger:hover .upload-icon-wrap { transform: scale(1.05); }
.upload-text { font-size: 14px; color: var(--text-color); font-weight: 500; }

/* 要求行：txt 在左，「填写」贴卡片右侧 */
.req-upload-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 12px;
  margin-top: 8px;
  width: 100%;
}
.req-upload-txt {
  flex: 0 1 auto;
  min-width: 0;
}
.upload-fill-btn {
  margin-left: auto;
  align-self: center;
  min-height: 32px;
  padding: 0 16px;
  font-size: 13px;
  font-weight: 600;
}

.upload-result {
  margin-top: 14px;
  padding: 12px 14px;
  background: rgba(103,197,164,0.1);
  border: 1px solid rgba(103,197,164,0.25);
  border-radius: 10px;
}
.upload-result-text { font-size: 13px; color: var(--text-color); margin-bottom: 8px; }
.upload-cols { display: flex; flex-wrap: wrap; gap: 6px; }

.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.mini-stats {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.mini-stat {
  flex: 1;
  min-width: 60px;
  text-align: center;
  padding: 12px 8px;
  background: rgba(255,255,255,0.3);
  border: 1px solid rgba(255,255,255,0.4);
  border-radius: 10px;
  backdrop-filter: blur(8px);
}
.mini-num { font-size: 20px; font-weight: 700; color: var(--text-color); transition: color 0.5s; }
.mini-label { font-size: 11px; color: var(--text-secondary); margin-top: 2px; transition: color 0.5s; }

.source-hint { margin-bottom: 12px; font-size: 13px; color: var(--text-secondary); }

.result-table-wrap {
  flex: 1;
  min-height: 220px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.result-table-wrap :deep(.el-table) { flex: 1; }

.upload-sheets { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.sheet-info { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; padding: 6px 10px; background: rgba(255,255,255,0.06); border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); }
.sheet-name { font-weight: 600; font-size: 13px; color: var(--primary-color); margin-right: 6px; white-space: nowrap; }

.history-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 8px;
}

.conf-badge {
  font-size: 12px; font-weight: 600;
  padding: 2px 6px; border-radius: 6px;
}
.conf-badge.high { color: #3a9e7a; background: rgba(103,197,164,0.15); }
.conf-badge.mid { color: #b08a20; background: rgba(232,184,73,0.15); }
.conf-badge.low { color: #c05050; background: rgba(224,112,112,0.15); }

.preview-row {
  display: flex; align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid rgba(255,255,255,0.3);
  gap: 12px;
}
.preview-label { width: 130px; font-weight: 500; color: var(--text-color); flex-shrink: 0; font-size: 13px; }
.preview-value { flex: 1; font-size: 13px; color: var(--text-color); }
.preview-value.empty { color: var(--text-secondary); font-style: italic; }
.preview-meta { width: 60px; text-align: right; font-size: 12px; flex-shrink: 0; }

@media (max-width: 900px) {
  .tpl-grid { grid-template-columns: 1fr; grid-template-rows: none; }
  .cell-tl, .cell-tr, .cell-bl, .cell-br { grid-column: 1; grid-row: auto; }
}
@media (max-width: 767px) {
  .mini-stat { min-width: calc(50% - 10px); }
  .preview-label { width: 90px; font-size: 12px; }
  .preview-value { font-size: 12px; }
}
</style>
