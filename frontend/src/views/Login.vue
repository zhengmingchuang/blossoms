<template>
  <div class="login-page">
    <AuthDynamicBackground />
    <div class="login-card card card-animate">
      <div class="login-header">
        <div class="login-logo-wrap">
          <img src="/logo.png" alt="DF" class="login-logo" @error="logoErr = true" v-if="!logoErr" />
          <span v-else class="login-logo-fallback">DF</span>
        </div>
        <h1>DocFusion</h1>
        <p>文档理解与多源数据融合系统</p>
      </div>
      <el-form :model="form" @submit.prevent="submit" class="login-form">
        <el-form-item>
          <el-input v-model="form.username" :placeholder="isLogin ? '账号 / 用户名' : '账号'" size="large" prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" placeholder="密码" type="password" size="large" prefix-icon="Lock" show-password />
        </el-form-item>
        <div v-if="isLogin" class="demo-tip" @click="fillDemo">
          <el-icon class="demo-tip-icon"><InfoFilled /></el-icon>
          <div class="demo-tip-text">
            测试账号：<b>admin</b>　密码：<b>admin123</b>
            <span class="demo-tip-action">点击自动填充</span>
          </div>
        </div>
        <el-button type="primary" size="large" :loading="loading" @click="submit" class="login-btn">
          {{ isLogin ? '登 录' : '注 册' }}
        </el-button>
        <div class="switch-link">
          <el-button link @click="isLogin = !isLogin" class="switch-btn">
            {{ isLogin ? '没有账号？去注册' : '已有账号？去登录' }}
          </el-button>
        </div>
      </el-form>
    </div>
    <div class="login-footer">DocFusion &copy; 2026</div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { authApi } from '../api'
import { ElMessage } from 'element-plus'
import { InfoFilled } from '@element-plus/icons-vue'
import AuthDynamicBackground from '../components/AuthDynamicBackground.vue'

const router = useRouter()
const form = ref({ username: '', password: '' })
const isLogin = ref(true)
const loading = ref(false)
const logoErr = ref(false)

const fillDemo = () => {
  form.value.username = 'admin'
  form.value.password = 'admin123'
}

const submit = async () => {
  if (!form.value.username || !form.value.password) return ElMessage.warning('请填写完整信息')
  loading.value = true
  try {
    if (isLogin.value) {
      const { data } = await authApi.login(form.value.username, form.value.password)
      sessionStorage.setItem('token', data.access_token)
      sessionStorage.setItem('user', JSON.stringify({ username: data.username, nickname: data.nickname || data.username, role: data.role, avatar: data.avatar || '' }))
      ElMessage.success('登录成功')
      router.push('/dashboard')
    } else {
      await authApi.register(form.value.username, form.value.password)
      ElMessage.success('注册成功，请登录')
      isLogin.value = true
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '操作失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.login-card {
  width: 420px;
  max-width: 92vw;
  padding: 48px 40px;
  position: relative;
  z-index: 1;
  animation: login-card-in 0.6s cubic-bezier(0.25, 0.8, 0.25, 1) both;
}

@keyframes login-card-in {
  from { opacity: 0; transform: translateY(32px) scale(0.96); filter: blur(4px); }
  to { opacity: 1; transform: translateY(0) scale(1); filter: blur(0); }
}

.login-header {
  text-align: center;
  margin-bottom: 36px;
}

.login-logo-wrap {
  width: 72px;
  height: 72px;
  border-radius: 20px;
  background: linear-gradient(135deg, var(--primary-color), var(--accent-color));
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 16px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.12);
  overflow: hidden;
}

.login-logo { width: 100%; height: 100%; object-fit: cover; }
.login-logo-fallback { color: #fff; font-size: 24px; font-weight: 700; }

.login-header h1 {
  font-size: 28px;
  font-weight: 700;
  color: var(--text-color);
  margin-bottom: 6px;
  letter-spacing: -0.5px;
  transition: color 0.5s;
}

.login-header p {
  font-size: 14px;
  color: var(--text-secondary);
  transition: color 0.5s;
}

.login-form { display: flex; flex-direction: column; gap: 4px; }

.demo-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  margin: 2px 0 10px;
  border-radius: 10px;
  background: linear-gradient(135deg, rgba(var(--primary-color-rgb, 64, 158, 255), 0.08), rgba(var(--accent-color-rgb, 64, 158, 255), 0.08));
  border: 1px dashed rgba(var(--primary-color-rgb, 64, 158, 255), 0.4);
  color: var(--text-secondary);
  font-size: 12.5px;
  line-height: 1.5;
  cursor: pointer;
  user-select: none;
  transition: background 0.25s, border-color 0.25s, transform 0.15s;
}
.demo-tip:hover {
  background: linear-gradient(135deg, rgba(var(--primary-color-rgb, 64, 158, 255), 0.14), rgba(var(--accent-color-rgb, 64, 158, 255), 0.14));
  border-color: var(--primary-color);
}
.demo-tip:active { transform: scale(0.99); }
.demo-tip-icon { color: var(--primary-color); font-size: 16px; flex-shrink: 0; }
.demo-tip-text { flex: 1; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.demo-tip-text b { color: var(--text-color); font-weight: 600; }
.demo-tip-action {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--primary-color);
  opacity: 0.85;
}
.demo-tip:hover .demo-tip-action { opacity: 1; }

.login-btn {
  width: 100%;
  margin-top: 8px;
  height: 46px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 2px;
  border-radius: 12px !important;
}

.switch-link { text-align: center; margin-top: 16px; }
.switch-btn { color: var(--primary-color) !important; font-size: 13px; }

.login-footer {
  position: absolute;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 12px;
  color: rgba(100,100,120,0.4);
  z-index: 1;
  white-space: nowrap;
}

@media (max-width: 480px) {
  .login-card { padding: 32px 24px; }
  .login-logo-wrap { width: 56px; height: 56px; border-radius: 16px; }
  .login-header h1 { font-size: 22px; }
}
</style>
