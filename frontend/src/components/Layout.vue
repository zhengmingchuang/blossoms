<template>
  <el-container class="app-layout">
    <!-- PC 侧栏 -->
    <el-aside v-if="!isMobile" width="220px" class="sidebar">
      <div class="logo">
        <img src="/logo.png" alt="Logo" class="logo-img" />
        <span>DocFusion</span>
      </div>
      <el-menu :default-active="$route.path" router class="sidebar-menu" background-color="transparent" :collapse="false">
        <el-menu-item index="/dashboard"><el-icon><DataAnalysis /></el-icon><span>工作台</span></el-menu-item>
        <el-menu-item index="/documents"><el-icon><Document /></el-icon><span>文档管理</span></el-menu-item>
        <el-menu-item index="/extraction"><el-icon><Search /></el-icon><span>信息提取</span></el-menu-item>
        <el-menu-item index="/template"><el-icon><EditPen /></el-icon><span>模板填报</span></el-menu-item>
        <el-menu-item index="/chat"><el-icon><ChatDotSquare /></el-icon><span>智能交互</span></el-menu-item>
        <el-menu-item index="/settings"><el-icon><Setting /></el-icon><span>系统设置</span></el-menu-item>
      </el-menu>
    </el-aside>
    <!-- 移动端抽屉侧栏 -->
    <el-drawer v-if="isMobile" v-model="drawerOpen" direction="ltr" size="220px" :show-close="false" :with-header="false" class="mobile-drawer">
      <div class="logo" style="padding:16px 20px;">
        <img src="/logo.png" alt="Logo" class="logo-img" />
        <span>DocFusion</span>
      </div>
      <el-menu :default-active="$route.path" router class="sidebar-menu" background-color="transparent" @select="drawerOpen = false">
        <el-menu-item index="/dashboard"><el-icon><DataAnalysis /></el-icon><span>工作台</span></el-menu-item>
        <el-menu-item index="/documents"><el-icon><Document /></el-icon><span>文档管理</span></el-menu-item>
        <el-menu-item index="/extraction"><el-icon><Search /></el-icon><span>信息提取</span></el-menu-item>
        <el-menu-item index="/template"><el-icon><EditPen /></el-icon><span>模板填报</span></el-menu-item>
        <el-menu-item index="/chat"><el-icon><ChatDotSquare /></el-icon><span>智能交互</span></el-menu-item>
        <el-menu-item index="/settings"><el-icon><Setting /></el-icon><span>系统设置</span></el-menu-item>
      </el-menu>
    </el-drawer>
    <el-container>
      <el-header class="topbar">
        <div class="topbar-left">
          <el-button v-if="isMobile" text @click="drawerOpen = true" class="hamburger-btn">
            <el-icon :size="22"><Fold /></el-icon>
          </el-button>
        </div>
        <div class="topbar-right">
          <img :src="userAvatar" class="topbar-avatar" />
          <span class="user-name">{{ username }}</span>
          <el-button text @click="logout">退出</el-button>
        </div>
      </el-header>
      <el-main class="main-area">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
    <CustomCursor />
    <ThemeSelector />
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import CustomCursor from './CustomCursor.vue'
import ThemeSelector from './ThemeSelector.vue'

const router = useRouter()
const avatarUrl = ref('')
const nicknameOverride = ref('')
const drawerOpen = ref(false)
const windowWidth = ref(window.innerWidth)
const isMobile = computed(() => windowWidth.value < 768)

const onResize = () => { windowWidth.value = window.innerWidth }

const username = computed(() => {
  if (nicknameOverride.value) return nicknameOverride.value
  try {
    const u = JSON.parse(sessionStorage.getItem('user') || '{}')
    return u.nickname || u.username || '用户'
  } catch { return '用户' }
})

const userAvatar = computed(() => {
  if (avatarUrl.value) return avatarUrl.value
  try {
    const a = JSON.parse(sessionStorage.getItem('user') || '{}').avatar
    if (a) return a
  } catch {}
  return '/default-avatar.jpg'
})

const onAvatarUpdated = (e) => { avatarUrl.value = e.detail }
const onNicknameUpdated = (e) => { nicknameOverride.value = e.detail }

const logout = () => { sessionStorage.clear(); router.push('/login') }

onMounted(() => {
  window.addEventListener('avatar-updated', onAvatarUpdated)
  window.addEventListener('nickname-updated', onNicknameUpdated)
  window.addEventListener('resize', onResize)
  try {
    const u = JSON.parse(sessionStorage.getItem('user') || '{}')
    if (u.avatar) avatarUrl.value = u.avatar
    if (u.nickname) nicknameOverride.value = u.nickname
  } catch {}
})
onUnmounted(() => {
  window.removeEventListener('avatar-updated', onAvatarUpdated)
  window.removeEventListener('nickname-updated', onNicknameUpdated)
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.app-layout { min-height: 100vh; }

.sidebar {
  background: var(--card-bg, rgba(255, 255, 255, 0.7));
  backdrop-filter: var(--card-backdrop, blur(20px));
  -webkit-backdrop-filter: var(--card-backdrop, blur(20px));
  border-right: 1px solid var(--card-border, rgba(200, 206, 214, 0.5));
  overflow-y: auto;
  box-shadow: 5px 0 25px rgba(0, 0, 0, 0.03), inset 1px 0 0 rgba(255, 255, 255, 0.2);
  transition: background 0.5s, border-color 0.5s;
}

.logo { display: flex; align-items: center; gap: 10px; padding: 20px; font-size: 20px; font-weight: 700; color: var(--primary); transition: color 0.4s; }
.logo-img { width: 36px; height: 36px; object-fit: contain; }

.sidebar-menu { border-right: none !important; }
.sidebar-menu .el-menu-item { border-radius: 10px; margin: 2px 8px; transition: all 0.25s; color: var(--text-secondary) !important; }
.sidebar-menu .el-menu-item.is-active {
  background: rgba(255, 255, 255, 0.5) !important;
  color: var(--primary) !important;
  font-weight: 600;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.6);
}
.sidebar-menu .el-menu-item:hover:not(.is-active) { background: rgba(255, 255, 255, 0.3) !important; }

.topbar {
  height: 60px;
  background: var(--card-bg, rgba(255, 255, 255, 0.7));
  backdrop-filter: var(--card-backdrop, blur(20px));
  -webkit-backdrop-filter: var(--card-backdrop, blur(20px));
  border-bottom: 1px solid var(--card-border, rgba(200, 206, 214, 0.5));
  display: flex; align-items: center; justify-content: space-between; padding: 0 24px;
  transition: background 0.5s, border-color 0.5s;
}

.topbar-left { display: flex; align-items: center; }
.topbar-right { display: flex; align-items: center; gap: 12px; }
.topbar-avatar { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid rgba(255, 255, 255, 0.6); box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08); }
.user-name { font-size: 14px; color: var(--text-secondary); transition: color 0.4s; }
.main-area { padding: 24px; background: transparent; overflow-y: auto; }
.hamburger-btn { padding: 4px; margin-right: 8px; }

@media (max-width: 767px) {
  .topbar { padding: 0 12px; height: 52px; }
  .main-area { padding: 12px; }
  .user-name { display: none; }
  .topbar-avatar { width: 28px; height: 28px; }
}
</style>

<style>
/* 移动端抽屉样式（非 scoped，覆盖 el-drawer） */
.mobile-drawer .el-drawer__body { padding: 0 !important; }
</style>
