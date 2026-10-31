<template>
  <div>
    <div class="page-header"><h2>系统设置</h2><p>个人资料、配置管理、日志查看</p></div>

    <el-tabs v-model="activeTab" class="glass-tabs">
      <!-- 个人资料 -->
      <el-tab-pane label="个人资料" name="profile">
        <div class="settings-grid">
          <div class="card card-animate">
            <h3 class="sec-title">个人信息</h3>
            <div class="profile-top">
              <div class="avatar-wrapper" @click="triggerAvatarUpload">
                <img :src="userAvatar" class="profile-avatar" @error="avatarErr = true" v-if="!avatarErr" />
                <span v-else class="avatar-fallback">{{ userInitial }}</span>
                <div class="avatar-overlay">更换头像</div>
                <input ref="avatarInput" type="file" accept=".jpg,.jpeg,.png,.gif,.webp" style="display:none;" @change="onAvatarChange" />
              </div>
              <div class="profile-meta">
                <div class="profile-name">{{ userInfo.nickname || userInfo.username }}</div>
                <el-tag size="small" :type="userInfo.role === 'admin' ? 'warning' : 'info'" style="margin-top:6px;">
                  {{ userInfo.role === 'admin' ? '管理员' : '普通用户' }}
                </el-tag>
              </div>
            </div>
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="用户ID">{{ userInfo.id }}</el-descriptions-item>
              <el-descriptions-item label="账号">{{ userInfo.username }}</el-descriptions-item>
              <el-descriptions-item label="用户名">
                <div class="nickname-row">
                  <template v-if="!editingNickname">
                    <span>{{ userInfo.nickname || userInfo.username }}</span>
                    <el-button link type="primary" size="small" @click="startEditNickname">修改</el-button>
                  </template>
                  <template v-else>
                    <el-input v-model="nicknameInput" size="small" style="width:160px;" placeholder="2-20个字符" maxlength="20" />
                    <el-button type="primary" size="small" :loading="nicknameSaving" @click="saveNickname">保存</el-button>
                    <el-button size="small" @click="editingNickname = false">取消</el-button>
                  </template>
                </div>
              </el-descriptions-item>
              <el-descriptions-item label="角色">{{ userInfo.role === 'admin' ? '管理员' : '普通用户' }}</el-descriptions-item>
            </el-descriptions>
          </div>

          <div class="card card-animate" style="animation-delay:0.08s">
            <h3 class="sec-title">安全设置</h3>
            <el-form :model="pwdForm" label-width="90px" label-position="left">
              <el-form-item label="原密码">
                <el-input v-model="pwdForm.old_password" type="password" show-password placeholder="请输入原密码" />
              </el-form-item>
              <el-form-item label="新密码">
                <el-input v-model="pwdForm.new_password" type="password" show-password placeholder="至少 6 位" />
              </el-form-item>
              <el-form-item label="确认密码">
                <el-input v-model="pwdForm.confirm_password" type="password" show-password placeholder="再次输入新密码" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="pwdSaving" @click="changePassword">修改密码</el-button>
              </el-form-item>
            </el-form>
          </div>
        </div>
      </el-tab-pane>

      <!-- 模型配置 -->
      <el-tab-pane label="模型配置" name="config" v-if="userInfo.role === 'admin'">
        <div class="settings-grid">
          <div class="card card-animate" style="grid-column: span 1;">
            <h3 class="sec-title">LLM 配置</h3>
            <div style="margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
              <span style="font-size:13px;color:var(--text-secondary);line-height:28px;">快捷预设：</span>
              <el-button size="small" type="primary" @click="applyPreset('deepseek')">DeepSeek（推荐）</el-button>
              <el-button size="small" @click="applyPreset('openai-compat')">OpenAI 兼容</el-button>
              <el-button size="small" @click="applyPreset('custom')">自定义</el-button>
            </div>
            <el-form :model="form" label-width="110px" label-position="left">
              <el-form-item label="API 地址"><el-input v-model="form.llm_api_base" placeholder="http://..." /></el-form-item>
              <el-form-item label="API Key">
                <el-input v-model="form.llm_api_key" :placeholder="hasKey ? '已配置（留空不修改）' : 'sk-...'" type="text" autocomplete="off" />
                <div v-if="hasKey" class="field-hint">留空则保持原有 Key 不变</div>
              </el-form-item>
              <el-form-item label="模型"><el-input v-model="form.llm_model" placeholder="deepseek-flash" /></el-form-item>
              <el-form-item label="API 格式">
                <el-radio-group v-model="form.llm_api_format">
                  <el-radio value="anthropic">Anthropic</el-radio>
                  <el-radio value="openai">OpenAI</el-radio>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="Embedding"><el-input v-model="form.embedding_model" placeholder="all-MiniLM-L6-v2" /></el-form-item>
              <el-form-item label="MCP 模式"><el-switch v-model="form.mcp_enabled" active-text="开启" inactive-text="关闭" /></el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="saving" @click="saveConfig">保存配置</el-button>
              </el-form-item>
            </el-form>
          </div>

          <div style="display:flex;flex-direction:column;gap:20px;">
            <div class="card card-animate" style="animation-delay:0.08s">
              <h3 class="sec-title">MCP 服务</h3>
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="协议">MCP (Model Context Protocol)</el-descriptions-item>
                <el-descriptions-item label="传输">HTTP + SSE</el-descriptions-item>
                <el-descriptions-item label="SSE">/api/mcp/sse</el-descriptions-item>
                <el-descriptions-item label="JSON-RPC">/api/mcp/jsonrpc</el-descriptions-item>
              </el-descriptions>
            </div>
            <div class="card card-animate" style="animation-delay:0.14s">
              <h3 class="sec-title">可用工具 ({{ tools.length }})</h3>
              <div v-for="t in tools" :key="t.name" class="tool-card">
                <div class="tool-name">{{ t.name }}</div>
                <div class="tool-desc">{{ t.description }}</div>
              </div>
              <el-empty v-if="!tools.length" description="暂无工具" :image-size="40" />
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- 用户管理 -->
      <el-tab-pane label="用户管理" name="users" v-if="userInfo.role === 'admin'">
        <div class="card card-animate">
          <el-table :data="users" size="small">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="username" label="账号" width="120" />
            <el-table-column prop="nickname" label="用户名" width="120" />
            <el-table-column prop="role" label="角色" width="120">
              <template #default>
                <el-tag size="small" type="warning">管理员</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="create_time" label="创建时间" />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <template v-if="row.username !== 'admin'">
                  <el-popconfirm title="删除用户？" @confirm="delUser(row)">
                    <template #reference><el-button link type="danger" size="small">删除</el-button></template>
                  </el-popconfirm>
                </template>
                <span v-else class="super-admin-label">超级管理员</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- 操作日志 -->
      <el-tab-pane label="操作日志" name="logs">
        <div class="card card-animate">
          <el-table :data="logs" size="small" v-loading="loadingLogs">
            <el-table-column prop="username" label="用户" width="100" />
            <el-table-column prop="action" label="操作" width="120" />
            <el-table-column prop="detail" label="详情" show-overflow-tooltip />
            <el-table-column prop="create_time" label="时间" width="170" />
          </el-table>
          <el-pagination
            v-if="logTotal > 30"
            style="margin-top:12px;justify-content:flex-end;"
            layout="total, prev, pager, next"
            :total="logTotal" :page-size="30"
            v-model:current-page="logPage" @current-change="loadLogs"
          />
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { settingsApi, mcpApi, authApi } from '../api'
import { ElMessage } from 'element-plus'

const activeTab = ref('profile')
const userInfo = ref({ id: '', username: '', nickname: '', role: '', avatar: '' })
const avatarInput = ref(null)
const avatarErr = ref(false)

const userInitial = computed(() => ((userInfo.value.nickname || userInfo.value.username || 'U')[0]).toUpperCase())
const userAvatar = computed(() => userInfo.value.avatar || '/default-avatar.jpg')

const editingNickname = ref(false)
const nicknameInput = ref('')
const nicknameSaving = ref(false)
const startEditNickname = () => { nicknameInput.value = userInfo.value.nickname || userInfo.value.username; editingNickname.value = true }
const saveNickname = async () => {
  if (!nicknameInput.value.trim() || nicknameInput.value.trim().length < 2) return ElMessage.warning('用户名至少 2 个字符')
  nicknameSaving.value = true
  try {
    await authApi.changeNickname(nicknameInput.value.trim())
    userInfo.value.nickname = nicknameInput.value.trim()
    editingNickname.value = false
    const stored = JSON.parse(sessionStorage.getItem('user') || '{}')
    stored.nickname = nicknameInput.value.trim()
    sessionStorage.setItem('user', JSON.stringify(stored))
    window.dispatchEvent(new CustomEvent('nickname-updated', { detail: nicknameInput.value.trim() }))
    ElMessage.success('用户名修改成功')
  } catch (e) { ElMessage.error(e.response?.data?.detail || '修改失败') }
  finally { nicknameSaving.value = false }
}

const pwdForm = ref({ old_password: '', new_password: '', confirm_password: '' })
const pwdSaving = ref(false)
const changePassword = async () => {
  if (!pwdForm.value.old_password) return ElMessage.warning('请输入原密码')
  if (!pwdForm.value.new_password || pwdForm.value.new_password.length < 6) return ElMessage.warning('新密码至少 6 位')
  if (pwdForm.value.new_password !== pwdForm.value.confirm_password) return ElMessage.warning('两次输入的密码不一致')
  pwdSaving.value = true
  try {
    await authApi.changePassword(pwdForm.value.old_password, pwdForm.value.new_password)
    ElMessage.success('密码修改成功')
    pwdForm.value = { old_password: '', new_password: '', confirm_password: '' }
  } catch (e) { ElMessage.error(e.response?.data?.detail || '修改失败') }
  finally { pwdSaving.value = false }
}

const loadProfile = async () => {
  try {
    const { data } = await authApi.me()
    userInfo.value = data; avatarErr.value = false
    const stored = JSON.parse(sessionStorage.getItem('user') || '{}')
    stored.avatar = data.avatar || ''; stored.nickname = data.nickname || data.username
    sessionStorage.setItem('user', JSON.stringify(stored))
  } catch {}
}

const triggerAvatarUpload = () => { avatarInput.value?.click() }
const onAvatarChange = async (e) => {
  const file = e.target.files?.[0]
  if (!file) return
  try {
    const { data } = await authApi.uploadAvatar(file)
    userInfo.value.avatar = data.avatar; avatarErr.value = false
    const stored = JSON.parse(sessionStorage.getItem('user') || '{}')
    stored.avatar = data.avatar; sessionStorage.setItem('user', JSON.stringify(stored))
    window.dispatchEvent(new CustomEvent('avatar-updated', { detail: data.avatar }))
    ElMessage.success('头像更新成功')
  } catch { ElMessage.error('头像上传失败') }
  e.target.value = ''
}

const form = ref({ llm_api_base: '', llm_api_key: '', llm_model: '', llm_api_format: 'anthropic', embedding_model: '', mcp_enabled: true })
const saving = ref(false)
const hasKey = ref(false)
const tools = ref([])
const users = ref([])
const logs = ref([])
const logTotal = ref(0)
const logPage = ref(1)
const loadingLogs = ref(false)

const loadConfig = async () => {
  try {
    const { data } = await settingsApi.get()
    hasKey.value = data.has_api_key || false
    form.value = { llm_api_base: data.llm_api_base || '', llm_api_key: '', llm_model: data.llm_model || '', llm_api_format: data.llm_api_format || 'anthropic', embedding_model: data.embedding_model || '', mcp_enabled: data.mcp_enabled ?? true }
  } catch {}
  try { tools.value = (await mcpApi.tools()).data.tools || [] } catch {}
}
const saveConfig = async () => {
  saving.value = true
  try {
    const p = { ...form.value }; if (!p.llm_api_key) delete p.llm_api_key
    await settingsApi.save(p); ElMessage.success('保存成功')
  } catch { ElMessage.error('保存失败') } finally { saving.value = false }
}
const applyPreset = (name) => {
  // 只保留当前实测可用的预设；失效的第三方中转端点已移除，避免填入后请求 404。
  const presets = {
    'deepseek': { llm_api_base: 'https://api.deepseek.com/anthropic', llm_model: 'deepseek-flash', llm_api_format: 'anthropic' },
    'openai-compat': { llm_api_base: 'https://api.deepseek.com', llm_model: 'deepseek-chat', llm_api_format: 'openai' },
    'custom': { llm_api_base: '', llm_model: '', llm_api_format: 'openai' },
  }
  const p = presets[name]
  if (p) {
    form.value.llm_api_base = p.llm_api_base
    form.value.llm_model = p.llm_model
    form.value.llm_api_format = p.llm_api_format
    form.value.llm_api_key = ''
    ElMessage.info(name === 'custom' ? '请填写自建/第三方兼容端点后保存' : '已填入预设，请填写 API Key 后点击保存')
  }
}
const loadUsers = async () => { try { users.value = (await settingsApi.users()).data.users } catch {} }
const toggleRole = async row => { try { await settingsApi.updateUser(row.id, { role: row.role === 'admin' ? 'user' : 'admin' }); loadUsers() } catch {} }
const delUser = async row => { try { await settingsApi.deleteUser(row.id); ElMessage.success('已删除'); loadUsers() } catch (e) { ElMessage.error(e.response?.data?.detail || '失败') } }
const loadLogs = async () => {
  loadingLogs.value = true
  try { const { data } = await settingsApi.logs({ page: logPage.value }); logs.value = data.logs; logTotal.value = data.total }
  catch {} finally { loadingLogs.value = false }
}

watch(activeTab, v => {
  if (v === 'users') loadUsers()
  if (v === 'logs') loadLogs()
  if (v === 'config') loadConfig()
})
onMounted(loadProfile)
</script>

<style scoped>
.settings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  align-items: start;
}

.sec-title { font-size: 15px; font-weight: 600; color: var(--text-color); margin-bottom: 16px; transition: color 0.5s; }

/* 头像区 */
.profile-top {
  display: flex;
  align-items: center;
  gap: 20px;
  margin-bottom: 20px;
}
.avatar-wrapper {
  position: relative;
  width: 72px; height: 72px;
  border-radius: 50%;
  overflow: hidden;
  cursor: pointer;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(0,0,0,0.1);
  border: 2px solid rgba(255,255,255,0.6);
}
.profile-avatar { width: 72px; height: 72px; border-radius: 50%; object-fit: cover; }
.avatar-fallback {
  width: 72px; height: 72px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--palette-1), var(--palette-2));
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 24px; font-weight: 700;
}
.avatar-overlay {
  position: absolute; inset: 0;
  background: rgba(0,0,0,0.45);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; opacity: 0;
  transition: opacity 0.2s;
  border-radius: 50%;
}
.avatar-wrapper:hover .avatar-overlay { opacity: 1; }

.profile-meta { display: flex; flex-direction: column; }
.profile-name { font-size: 18px; font-weight: 600; color: var(--text-color); transition: color 0.5s; }

.nickname-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

.field-hint { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }

/* 工具卡片 */
.tool-card {
  padding: 10px 14px;
  background: rgba(255,255,255,0.3);
  border: 1px solid rgba(255,255,255,0.4);
  border-radius: 10px;
  margin-bottom: 8px;
  transition: background 0.2s;
}
.tool-card:hover { background: rgba(255,255,255,0.5); }
.tool-name { font-size: 13px; font-weight: 600; color: var(--primary-color); transition: color 0.5s; }
.tool-desc { font-size: 12px; color: var(--text-secondary); margin-top: 2px; transition: color 0.5s; }

.super-admin-label { font-size: 12px; color: var(--text-secondary); }

@media (max-width: 900px) {
  .settings-grid { grid-template-columns: 1fr; }
}
@media (max-width: 767px) {
  .profile-top { gap: 14px; }
  .avatar-wrapper, .profile-avatar { width: 56px !important; height: 56px !important; }
  .profile-name { font-size: 16px; }
}
</style>
