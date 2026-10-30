<template>
  <div class="dashboard">
    <div class="page-header">
      <h2>工作台</h2>
      <p>系统数据总览与任务监控</p>
    </div>

    <!-- 统计卡片 -->
    <div class="stat-grid">
      <div
        v-for="(s, i) in statCards"
        :key="s.label"
        class="stat-card card card-animate"
        :class="{ clickable: !!s.route }"
        :style="{ animationDelay: i * 0.06 + 's' }"
        @click="s.route && router.push(s.route)"
      >
        <div class="stat-icon" :style="{ background: `linear-gradient(135deg, ${s.color}22, ${s.color}44)`, border: `1px solid ${s.color}33` }">
          <el-icon :size="22" :color="s.color"><component :is="s.icon" /></el-icon>
        </div>
        <div class="stat-info">
          <div class="stat-num" :style="{ color: s.color }">{{ s.value }}</div>
          <div class="stat-label">{{ s.label }}</div>
        </div>
      </div>
    </div>

    <!-- 功能模块导航 -->
    <div class="card card-animate" style="margin-bottom:20px; animation-delay:0.28s">
      <div class="modules-header">
        <h3 class="sec-title" style="margin-bottom:0;">功能模块</h3>
        <span class="modules-sub">点击卡片快速进入对应功能</span>
      </div>
      <div class="modules-grid">
        <div
          v-for="(m, i) in modules"
          :key="m.route"
          class="module-card"
          :style="{ animationDelay: (0.32 + i * 0.04) + 's', '--m-color': m.color }"
          @click="router.push(m.route)"
        >
          <div class="module-icon">
            <el-icon :size="20" :color="m.color"><component :is="m.icon" /></el-icon>
          </div>
          <div class="module-body">
            <div class="module-title">{{ m.title }}</div>
            <div class="module-desc">{{ m.desc }}</div>
            <ul class="module-tips">
              <li v-for="t in m.tips" :key="t">{{ t }}</li>
            </ul>
          </div>
          <div class="module-cta">进入 →</div>
        </div>
      </div>
    </div>

    <!-- 下方两列 -->
    <div class="bottom-grid">
      <!-- 文件类型分布 -->
      <div class="card card-animate clickable" style="animation-delay:0.35s" @click="router.push('/documents')">
        <h3 class="sec-title">文件类型分布</h3>
        <div v-if="Object.keys(stats.file_type_stats || {}).length" class="type-list">
          <div v-for="(c, t) in stats.file_type_stats" :key="t" class="type-item">
            <span class="type-name">.{{ t }}</span>
            <div class="type-bar-wrap">
              <div
                class="type-bar"
                :style="{
                  width: Math.round(c / Math.max(stats.file_count, 1) * 100) + '%',
                  background: typeColor(t)
                }"
              />
            </div>
            <span class="type-count">{{ c }} 个</span>
          </div>
        </div>
        <el-empty v-else description="暂无文件" :image-size="50" />
      </div>

      <!-- 最近上传 -->
      <div class="card card-animate clickable" style="animation-delay:0.42s" @click="router.push('/documents')">
        <h3 class="sec-title">最近上传</h3>
        <el-table :data="stats.recent_files || []" size="small">
          <el-table-column prop="file_name" label="文件名" show-overflow-tooltip />
          <el-table-column prop="file_type" label="类型" width="70">
            <template #default="{ row }"><el-tag size="small" type="info">.{{ row.file_type }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="parse_status" label="状态" width="80">
            <template #default="{ row }"><el-tag size="small" :type="stType(row.parse_status)">{{ stLabel(row.parse_status) }}</el-tag></template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <!-- 最近任务 -->
    <div class="card card-animate" style="animation-delay:0.5s; margin-top: 20px;">
      <div class="task-header">
        <h3 class="sec-title" style="margin-bottom:0;">最近任务</h3>
        <div class="task-actions" @click.stop>
          <el-button
            v-if="taskSelection.length"
            type="danger"
            size="small"
            @click="batchDeleteTasks"
          >批量删除 ({{ taskSelection.length }})</el-button>
        </div>
      </div>
      <el-table
        :data="pagedTasks"
        size="small"
        @selection-change="taskSelection = $event"
        row-key="id"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column prop="task_name" label="任务" show-overflow-tooltip min-width="180" />
        <el-table-column prop="task_type" label="类型" width="80">
          <template #default="{ row }"><el-tag size="small">{{ taskLabel(row.task_type) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }"><el-tag size="small" :type="stType(row.status)">{{ stLabel(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="用时" width="80">
          <template #default="{ row }">{{ row.elapsed ? row.elapsed + 's' : '-' }}</template>
        </el-table-column>
        <el-table-column label="准确率" width="80">
          <template #default="{ row }">{{ row.accuracy ? (row.accuracy * 100).toFixed(0) + '%' : '-' }}</template>
        </el-table-column>
        <el-table-column prop="result_summary" label="结果" show-overflow-tooltip />
        <el-table-column prop="start_time" label="时间" width="160" />
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button
              type="danger"
              link
              size="small"
              :disabled="row.status === 'running'"
              @click.stop="deleteTask(row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="(stats.recent_tasks || []).length > taskPageSize"
        style="margin-top:12px;justify-content:flex-end;"
        layout="total, prev, pager, next"
        :total="(stats.recent_tasks || []).length"
        :page-size="taskPageSize"
        v-model:current-page="taskPage"
        size="small"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { dashApi, taskApi } from '../api'

const router = useRouter()
const stats = ref({})

// 最近任务分页 & 多选
const taskPage = ref(1)
const taskPageSize = 5
const taskSelection = ref([])
const pagedTasks = computed(() => {
  const all = stats.value.recent_tasks || []
  return all.slice((taskPage.value - 1) * taskPageSize, taskPage.value * taskPageSize)
})

const refreshStats = async () => {
  try { stats.value = (await dashApi.stats()).data } catch {}
}

const deleteTask = async (row) => {
  try {
    await ElMessageBox.confirm(`确定删除任务「${row.task_name || '未命名'}」？`, '删除任务', { type: 'warning' })
  } catch { return }
  try {
    await taskApi.delete(row.id)
    ElMessage.success('已删除')
    taskSelection.value = taskSelection.value.filter(t => t.id !== row.id)
    await refreshStats()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

const batchDeleteTasks = async () => {
  const count = taskSelection.value.length
  if (!count) return
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${count} 条任务？`, '批量删除', { type: 'warning' })
  } catch { return }
  try {
    const { data } = await taskApi.batchDelete(taskSelection.value.map(r => r.id))
    ElMessage.success(data?.message || `已删除 ${count} 条任务`)
    taskSelection.value = []
    await refreshStats()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

const modules = [
  {
    title: '文档管理',
    route: '/documents',
    icon: 'Document',
    color: 'var(--palette-1)',
    desc: '统一管理所有上传的文档，支持批量上传与解析。',
    tips: [
      '支持 docx / xlsx / md / txt 等多种格式',
      '上传后自动后台解析为可检索的片段',
      '可对解析完成的文档执行 AI 字段提取',
    ],
  },
  {
    title: '信息提取',
    route: '/extraction',
    icon: 'Search',
    color: 'var(--palette-3)',
    desc: '查看每个文档抽取出的字段、来源证据与多源融合结果。',
    tips: [
      '「按文件查看」直观浏览每个文件的字段',
      '「提取结果」按字段筛选、修正与批量删除',
      '「融合结果」对多个文件的同名字段进行加权汇总',
    ],
  },
  {
    title: '模板填报',
    route: '/template',
    icon: 'EditPen',
    color: 'var(--palette-4)',
    desc: '上传 Word/Excel 模板，自动用文档库中的数据完成填写。',
    tips: [
      '支持「字段匹配」与「表格批量」两种填报模式',
      '可在「数据源」直接上传表格作为填充来源',
      '填写结果带置信度评估，可一键导出文件',
    ],
  },
  {
    title: '智能交互',
    route: '/chat',
    icon: 'ChatDotRound',
    color: 'var(--palette-2)',
    desc: '基于文档内容的智能问答，支持多轮对话与文件上下文。',
    tips: [
      '可挂载已上传的文档作为对话上下文',
      '支持会话历史保存与多文件协同分析',
      '内置 LLM 直答与文档检索两种模式',
    ],
  },
  {
    title: '系统设置',
    route: '/settings',
    icon: 'Setting',
    color: 'var(--palette-1)',
    desc: '配置 LLM、向量模型、字段词典与用户管理等系统参数。',
    tips: [
      '配置 LLM 接口（API Base / Key / 模型名）',
      '维护字段词典以提升提取与匹配的准确率',
      '管理用户账号、查看系统操作日志',
    ],
  },
]

const statCards = computed(() => [
  { label: '文件总数', value: stats.value.file_count || 0, icon: 'Document', color: 'var(--palette-1)', route: '/documents' },
  { label: '已解析', value: stats.value.parsed_count || 0, icon: 'CircleCheck', color: 'var(--palette-2)', route: '/documents' },
  { label: '提取字段', value: stats.value.extract_count || 0, icon: 'Search', color: 'var(--palette-3)', route: '/extraction' },
  { label: '模板数', value: stats.value.template_count || 0, icon: 'EditPen', color: 'var(--palette-4)', route: '/template' },
  { label: '平均用时', value: (stats.value.avg_time || 0) + 's', icon: 'Timer', color: 'var(--palette-1)', route: null },
  { label: '成功率', value: (stats.value.success_rate || 0) + '%', icon: 'TrendCharts', color: 'var(--palette-2)', route: null },
])

const typeColor = t => ({ docx: 'var(--palette-1)', xlsx: 'var(--palette-2)', md: 'var(--palette-3)', txt: 'var(--palette-4)' }[t] || 'var(--palette-4)')
const stType = s => ({ done: 'success', error: 'danger', running: 'warning', parsing: 'warning', pending: 'info' }[s] || 'info')
const stLabel = s => ({ done: '完成', error: '失败', running: '运行中', parsing: '解析中', pending: '等待' }[s] || s)
const taskLabel = t => ({ parse: '解析', reparse: '重解析', fill: '填写' }[t] || t)

onMounted(refreshStats)
</script>

<style scoped>
.dashboard { display: flex; flex-direction: column; gap: 0; }

.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
  margin-bottom: 20px;
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 16px;
  cursor: default;
  transition: transform 0.25s, box-shadow 0.25s;
}
.stat-card.clickable { cursor: pointer; }
.stat-card:hover { transform: translateY(-3px); box-shadow: 0 8px 24px rgba(0,0,0,0.08) !important; }
.stat-card.clickable:hover .stat-label::after {
  content: ' →';
  opacity: 0.5;
}

.stat-icon {
  width: 46px;
  height: 46px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: transform 0.25s;
}
.stat-card:hover .stat-icon { transform: scale(1.08); }

.stat-num { font-size: 22px; font-weight: 700; line-height: 1.2; transition: color 0.5s; }
.stat-label { font-size: 12px; color: var(--text-secondary); margin-top: 2px; transition: color 0.5s; }

.bottom-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 0;
}

.modules-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 14px;
}
.modules-sub { font-size: 12px; color: var(--text-secondary); }

.modules-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
}
.module-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 14px 18px;
  border-radius: 14px;
  background: rgba(255,255,255,0.45);
  border: 1px solid rgba(255,255,255,0.55);
  cursor: pointer;
  overflow: hidden;
  transition: transform 0.25s, box-shadow 0.25s, border-color 0.25s;
  animation: fadeUp 0.6s ease both;
}
.module-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto 0;
  height: 3px;
  background: var(--m-color, var(--primary-color));
  opacity: 0.6;
  transition: height 0.25s, opacity 0.25s;
}
.module-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 10px 28px rgba(0,0,0,0.08);
  border-color: var(--m-color, var(--primary-color));
}
.module-card:hover::before { height: 5px; opacity: 1; }

.module-icon {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255,255,255,0.6);
  border: 1px solid rgba(255,255,255,0.7);
}
.module-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-color);
}
.module-desc {
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-secondary);
  margin-top: 4px;
}
.module-tips {
  margin: 6px 0 0;
  padding-left: 16px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.65;
  flex: 1;
}
.module-tips li { list-style: disc; margin: 2px 0; }
.module-cta {
  margin-top: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--m-color, var(--primary-color));
  align-self: flex-end;
  opacity: 0.8;
  transition: transform 0.25s, opacity 0.25s;
}
.module-card:hover .module-cta { transform: translateX(4px); opacity: 1; }

@keyframes fadeUp {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.sec-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-color);
  margin-bottom: 16px;
  transition: color 0.5s;
}

.clickable {
  cursor: pointer;
  transition: transform 0.25s, box-shadow 0.25s !important;
}
.clickable:hover {
  transform: translateY(-3px) !important;
  box-shadow: 0 8px 24px rgba(0,0,0,0.08) !important;
}

.task-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.task-actions { display: flex; gap: 8px; }

.type-list { display: flex; flex-direction: column; gap: 14px; }
.type-item { display: flex; align-items: center; gap: 10px; }
.type-name { font-size: 13px; font-weight: 600; width: 42px; color: var(--text-color); flex-shrink: 0; }
.type-bar-wrap { flex: 1; height: 8px; background: rgba(255,255,255,0.4); border-radius: 10px; overflow: hidden; }
.type-bar { height: 100%; border-radius: 10px; transition: width 0.8s cubic-bezier(0.25, 0.8, 0.25, 1); }
.type-count { font-size: 12px; color: var(--text-secondary); width: 36px; text-align: right; flex-shrink: 0; }

@media (max-width: 1200px) {
  .stat-grid { grid-template-columns: repeat(3, 1fr); }
  .modules-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 767px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  .modules-grid { grid-template-columns: repeat(1, 1fr); }
  .bottom-grid { grid-template-columns: 1fr; }
  .stat-card { padding: 12px; gap: 10px; }
  .stat-icon { width: 38px; height: 38px; }
  .stat-num { font-size: 18px; }
}
</style>
