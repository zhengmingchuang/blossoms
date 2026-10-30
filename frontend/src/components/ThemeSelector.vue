<template>
  <div class="theme-selector-wrapper">
    <transition name="theme-pop">
      <div v-if="isOpen" class="theme-panel">
        <!-- List View -->
        <template v-if="!isEditing">
          <div class="tp-header">
            <h3>选择主题</h3>
            <div class="tp-actions">
              <button class="tp-icon-btn" @click="resetDefaults" title="重置预设">
                <el-icon><RefreshLeft /></el-icon>
              </button>
              <button class="tp-icon-btn" @click="startCreating" title="新建主题">
                <el-icon><Plus /></el-icon>
              </button>
            </div>
          </div>

          <div class="tp-section">
            <h4>预设主题</h4>
            <div class="tp-list">
              <div
                v-for="t in defaultThemes" :key="t.id"
                class="tp-item" :class="{ active: activeThemeId === t.id }"
                @click="applyThemeById(t.id)"
              >
                <div class="tp-swatch" :style="swatchStyle(t)" />
                <span class="tp-name">{{ t.name }}</span>
                <el-icon v-if="activeThemeId === t.id" class="tp-check"><Check /></el-icon>
                <button class="tp-tiny-btn" @click.stop="startEditing(t)"><el-icon><Edit /></el-icon></button>
                <button class="tp-tiny-btn danger" @click.stop="deleteTheme(t.id, true)"><el-icon><Delete /></el-icon></button>
              </div>
            </div>
          </div>

          <div v-if="customThemes.length" class="tp-section">
            <h4>自定义主题</h4>
            <div class="tp-list">
              <div
                v-for="t in customThemes" :key="t.id"
                class="tp-item" :class="{ active: activeThemeId === t.id }"
                @click="applyThemeById(t.id)"
              >
                <div class="tp-swatch" :style="swatchStyle(t)" />
                <span class="tp-name">{{ t.name }}</span>
                <el-icon v-if="activeThemeId === t.id" class="tp-check"><Check /></el-icon>
                <button class="tp-tiny-btn" @click.stop="startEditing(t)"><el-icon><Edit /></el-icon></button>
                <button class="tp-tiny-btn danger" @click.stop="deleteTheme(t.id, false)"><el-icon><Delete /></el-icon></button>
              </div>
            </div>
          </div>

          <!-- 光标开关 -->
          <div class="tp-cursor-row">
            <span class="tp-cursor-label">
              <el-icon style="margin-right:5px;vertical-align:-2px"><Pointer /></el-icon>自定义鼠标跟随
            </span>
            <button class="tp-cursor-toggle" :class="{ on: cursorEnabled }" @click="toggleCursor">
              <span class="tp-cursor-thumb"></span>
            </button>
          </div>
        </template>

        <!-- Edit View -->
        <template v-else>
          <div class="tp-header">
            <h3>编辑主题</h3>
            <button class="tp-icon-btn" @click="cancelEditing"><el-icon><Close /></el-icon></button>
          </div>

          <div class="tp-tabs">
            <button v-for="tab in editTabs" :key="tab.key" :class="{ active: editTab === tab.key }" @click="editTab = tab.key">{{ tab.label }}</button>
          </div>

          <div class="tp-edit-body">
            <template v-if="editTab === 'global'">
              <div class="tp-field">
                <label>主题名称</label>
                <input v-model="editingTheme.name" class="tp-input" />
              </div>
              <div class="tp-field">
                <label>基础颜色</label>
                <div class="tp-color-grid">
                  <div v-for="c in baseColors" :key="c.key" class="tp-color-item">
                    <input type="color" :value="editingTheme.colors[c.key]" @input="updateColor(c.key, $event.target.value)" />
                    <span>{{ c.label }}</span>
                  </div>
                </div>
              </div>
              <div class="tp-field">
                <label>背景渐变</label>
                <div class="tp-gradient-row">
                  <div v-for="i in 4" :key="i" class="tp-gradient-item">
                    <input type="color" :value="editingTheme.colors[`--bg-gradient-${i}`]" @input="updateColor(`--bg-gradient-${i}`, $event.target.value)" class="tp-color-round" />
                    <span>#{{ i }}</span>
                  </div>
                </div>
              </div>
            </template>

            <template v-if="editTab === 'cards'">
              <div class="tp-hint">调整毛玻璃卡片样式，使用 rgba() 实现透明效果。</div>
              <div class="tp-field">
                <label>卡片边框</label>
                <input v-model="editingTheme.colors['--card-border']" class="tp-input mono" @input="liveApply" />
              </div>
              <div class="tp-field">
                <label>卡片背景 (CSS)</label>
                <textarea v-model="editingTheme.colors['--card-bg']" class="tp-textarea mono" rows="3" @input="liveApply" />
              </div>
            </template>

            <template v-if="editTab === 'palette'">
              <div class="tp-hint">自定义数据可视化使用的 4 个配色。</div>
              <div class="tp-palette-list">
                <div v-for="i in 4" :key="i" class="tp-palette-item">
                  <span class="tp-palette-label">#{{ i }}</span>
                  <input type="color" :value="editingTheme.colors[`--palette-${i}`]" @input="updateColor(`--palette-${i}`, $event.target.value)" />
                  <div class="tp-palette-bar" :style="{ background: editingTheme.colors[`--palette-${i}`] }" />
                </div>
              </div>
            </template>
          </div>

          <button class="tp-save-btn" @click="saveTheme">
            <el-icon><Check /></el-icon> 保存主题
          </button>
        </template>
      </div>
    </transition>

    <button class="tp-fab" :class="{ open: isOpen }" @click="isOpen = !isOpen">
      <el-icon :size="20"><Brush /></el-icon>
    </button>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'

const STORAGE_KEYS = {
  custom: 'docfusion_custom_themes',
  defaults: 'docfusion_default_themes',
  active: 'docfusion_active_theme_id',
}

const createTheme = (id, name, base) => ({
  id,
  name,
  colors: {
    '--card-bg': 'rgba(255, 255, 255, 0.7)',
    '--card-border': 'rgba(200, 206, 214, 0.5)',
    '--card-shadow': '0 4px 24px rgba(0, 0, 0, 0.06), 0 0 0 1px rgba(255, 255, 255, 0.3) inset',
    '--card-backdrop': 'blur(20px)',
    '--palette-1': base['--primary-color'],
    '--palette-2': base['--accent-color'] || base['--primary-color'],
    '--palette-3': base['--primary-color'],
    '--palette-4': base['--accent-color'] || base['--primary-color'],
    ...base,
  },
})

const INITIAL_DEFAULTS = [
  createTheme('docfusion', 'DocFusion', {
    '--bg-color': '#f5f7fa',
    '--bg-gradient-1': 'hsla(228, 40%, 92%, 1)',
    '--bg-gradient-2': 'hsla(340, 30%, 94%, 1)',
    '--bg-gradient-3': 'hsla(150, 30%, 92%, 1)',
    '--bg-gradient-4': 'hsla(40, 40%, 92%, 1)',
    '--primary-color': '#5b6abf',
    '--accent-color': '#8a95d0',
    '--text-color': '#303133',
    '--text-secondary': '#909399',
    '--palette-1': '#5b6abf',
    '--palette-2': '#67c5a4',
    '--palette-3': '#e8b849',
    '--palette-4': '#e07070',
  }),
  createTheme('morandi', 'Morandi', {
    '--bg-color': '#f4f7f9',
    '--bg-gradient-1': '#e0e7ef',
    '--bg-gradient-2': '#f0e6ea',
    '--bg-gradient-3': '#dbece5',
    '--bg-gradient-4': '#ebe7dc',
    '--primary-color': '#7a8ca5',
    '--accent-color': '#a8b5c6',
    '--text-color': '#4A5568',
    '--text-secondary': '#718096',
    '--palette-1': '#7a8ca5',
    '--palette-2': '#a8b5c6',
    '--palette-3': '#96B0C4',
    '--palette-4': '#C5BCB1',
  }),
  createTheme('warm-earth', 'Warm Earth', {
    '--bg-color': '#f3efe5',
    '--bg-gradient-1': '#d6ddd4',
    '--bg-gradient-2': '#becac3',
    '--bg-gradient-3': '#cabab4',
    '--bg-gradient-4': '#b7a79a',
    '--primary-color': '#8aa5a0',
    '--accent-color': '#c7b1a5',
    '--text-color': '#4f4d47',
    '--text-secondary': '#837d75',
    '--palette-1': '#8aa5a0',
    '--palette-2': '#c7b1a5',
    '--palette-3': '#a0938b',
    '--palette-4': '#d1c7bd',
  }),
  createTheme('dark-mode', 'Dark Mode', {
    '--bg-color': '#0D1117',
    '--bg-gradient-1': '#161B22',
    '--bg-gradient-2': '#0D1117',
    '--bg-gradient-3': '#161B22',
    '--bg-gradient-4': '#30363D',
    '--primary-color': '#2F81F7',
    '--accent-color': '#3FB950',
    '--text-color': '#E6EDF3',
    '--text-secondary': '#848D97',
    '--card-bg': 'rgba(22, 27, 34, 0.75)',
    '--card-border': 'rgba(48, 54, 61, 0.7)',
    '--card-shadow': '0 8px 24px rgba(0, 0, 0, 0.2)',
    '--palette-1': '#2F81F7',
    '--palette-2': '#3FB950',
    '--palette-3': '#A371F7',
    '--palette-4': '#F0883E',
  }),
  createTheme('nordic', 'Nordic Wood', {
    '--bg-color': '#E8E6E1',
    '--bg-gradient-1': '#D6D2CD',
    '--bg-gradient-2': '#E8E6E1',
    '--bg-gradient-3': '#C9C5BE',
    '--bg-gradient-4': '#E0DCD8',
    '--primary-color': '#5D5C61',
    '--accent-color': '#8C8A84',
    '--text-color': '#373737',
    '--text-secondary': '#6E6E6E',
    '--card-bg': 'rgba(255, 255, 255, 0.5)',
    '--card-border': 'rgba(255, 255, 255, 0.6)',
    '--card-shadow': '0 4px 12px rgba(0, 0, 0, 0.05)',
    '--palette-1': '#8C8A84',
    '--palette-2': '#B7B4AD',
    '--palette-3': '#73716B',
    '--palette-4': '#A3A09B',
  }),
  createTheme('lavender', 'Lavender Dream', {
    '--bg-color': '#F3F0FF',
    '--bg-gradient-1': '#E9D5FF',
    '--bg-gradient-2': '#F3F0FF',
    '--bg-gradient-3': '#DDD6FE',
    '--bg-gradient-4': '#F5F3FF',
    '--primary-color': '#7C3AED',
    '--accent-color': '#A78BFA',
    '--text-color': '#4C1D95',
    '--text-secondary': '#6D28D9',
    '--card-bg': 'rgba(255, 255, 255, 0.45)',
    '--card-border': 'rgba(255, 255, 255, 0.6)',
    '--card-shadow': '0 8px 32px rgba(124, 58, 237, 0.08)',
    '--palette-1': '#8B5CF6',
    '--palette-2': '#D8B4FE',
    '--palette-3': '#C4B5FD',
    '--palette-4': '#A78BFA',
  }),
  createTheme('mint', 'Fresh Mint', {
    '--bg-color': '#F0FDFA',
    '--bg-gradient-1': '#CCFBF1',
    '--bg-gradient-2': '#F0FDFA',
    '--bg-gradient-3': '#99F6E4',
    '--bg-gradient-4': '#E0F2F1',
    '--primary-color': '#0D9488',
    '--accent-color': '#2DD4BF',
    '--text-color': '#115E59',
    '--text-secondary': '#134E4A',
    '--card-bg': 'rgba(255, 255, 255, 0.5)',
    '--card-border': 'rgba(255, 255, 255, 0.7)',
    '--card-shadow': '0 8px 24px rgba(13, 148, 136, 0.06)',
    '--palette-1': '#14B8A6',
    '--palette-2': '#5EEAD4',
    '--palette-3': '#2DD4BF',
    '--palette-4': '#99F6E4',
  }),
  createTheme('cyberpunk', 'Cyberpunk', {
    '--bg-color': '#000000',
    '--bg-gradient-1': '#120024',
    '--bg-gradient-2': '#001424',
    '--bg-gradient-3': '#240012',
    '--bg-gradient-4': '#000000',
    '--primary-color': '#00ff9f',
    '--accent-color': '#ff003c',
    '--text-color': '#ffffff',
    '--text-secondary': '#b3b3b3',
    '--card-bg': 'rgba(18, 18, 18, 0.8)',
    '--card-border': 'rgba(0, 255, 159, 0.3)',
    '--card-shadow': '0 0 20px rgba(0, 255, 159, 0.1)',
    '--palette-1': '#00ff9f',
    '--palette-2': '#ff003c',
    '--palette-3': '#00b8ff',
    '--palette-4': '#f6ff00',
  }),
]

const isOpen = ref(false)
const isEditing = ref(false)
const editTab = ref('global')
const activeThemeId = ref('docfusion')
const defaultThemes = ref([...INITIAL_DEFAULTS])
const customThemes = ref([])
const editingTheme = ref(null)
const cursorEnabled = ref(localStorage.getItem('docfusion_custom_cursor') !== 'false')

const toggleCursor = () => {
  cursorEnabled.value = !cursorEnabled.value
  localStorage.setItem('docfusion_custom_cursor', String(cursorEnabled.value))
  window.dispatchEvent(new CustomEvent('cursor-toggle', { detail: cursorEnabled.value }))
}

const editTabs = [
  { key: 'global', label: '基础' },
  { key: 'cards', label: '卡片' },
  { key: 'palette', label: '配色' },
]

const baseColors = [
  { key: '--primary-color', label: '主色' },
  { key: '--accent-color', label: '强调色' },
  { key: '--bg-color', label: '背景' },
  { key: '--text-color', label: '文字' },
]

const swatchStyle = (t) => ({
  background: `linear-gradient(135deg, ${t.colors['--bg-gradient-1']}, ${t.colors['--bg-gradient-2']})`,
})

const allThemes = () => [...defaultThemes.value, ...customThemes.value]

const findTheme = (id) => allThemes().find(t => t.id === id) || defaultThemes.value[0]

const applyThemeToDOM = (colors) => {
  const root = document.documentElement
  Object.entries(colors).forEach(([key, value]) => {
    root.style.setProperty(key, value)
  })
  if (colors['--primary-color']) {
    root.style.setProperty('--primary', colors['--primary-color'])
    root.style.setProperty('--el-color-primary', colors['--primary-color'])
  }
  if (colors['--text-color']) {
    root.style.setProperty('--text', colors['--text-color'])
    root.style.setProperty('--el-text-color-primary', colors['--text-color'])
    root.style.setProperty('--el-text-color-regular', colors['--text-color'])
  }
  if (colors['--text-secondary']) {
    root.style.setProperty('--text-secondary', colors['--text-secondary'])
    root.style.setProperty('--text-secondary-color', colors['--text-secondary'])
    root.style.setProperty('--el-text-color-secondary', colors['--text-secondary'])
  }
  if (colors['--bg-color']) {
    root.style.setProperty('--bg', colors['--bg-color'])
    root.style.setProperty('--el-bg-color', colors['--bg-color'])
  }
}

const applyThemeById = (id) => {
  activeThemeId.value = id
  const theme = findTheme(id)
  applyThemeToDOM(theme.colors)
  localStorage.setItem(STORAGE_KEYS.active, id)
}

const startEditing = (theme) => {
  editingTheme.value = JSON.parse(JSON.stringify(theme))
  isEditing.value = true
  editTab.value = 'global'
  applyThemeToDOM(theme.colors)
}

const startCreating = () => {
  const base = findTheme(activeThemeId.value)
  editingTheme.value = {
    ...JSON.parse(JSON.stringify(base)),
    id: '',
    name: '新主题',
    isCustom: true,
  }
  isEditing.value = true
  editTab.value = 'global'
}

const cancelEditing = () => {
  isEditing.value = false
  editingTheme.value = null
  applyThemeToDOM(findTheme(activeThemeId.value).colors)
}

const updateColor = (key, value) => {
  if (!editingTheme.value) return
  editingTheme.value.colors[key] = value
  document.documentElement.style.setProperty(key, value)
  if (key === '--primary-color') document.documentElement.style.setProperty('--primary', value)
  if (key === '--text-color') document.documentElement.style.setProperty('--text', value)
  if (key === '--bg-color') document.documentElement.style.setProperty('--bg', value)
}

const liveApply = () => {
  if (!editingTheme.value) return
  applyThemeToDOM(editingTheme.value.colors)
}

const saveTheme = () => {
  if (!editingTheme.value) return
  const isDefault = defaultThemes.value.some(t => t.id === editingTheme.value.id)

  if (isDefault) {
    defaultThemes.value = defaultThemes.value.map(t => t.id === editingTheme.value.id ? { ...editingTheme.value } : t)
    localStorage.setItem(STORAGE_KEYS.defaults, JSON.stringify(defaultThemes.value))
  } else {
    const toSave = {
      ...editingTheme.value,
      id: editingTheme.value.id || `custom-${Date.now()}`,
      isCustom: true,
    }
    const exists = customThemes.value.find(t => t.id === toSave.id)
    if (exists) {
      customThemes.value = customThemes.value.map(t => t.id === toSave.id ? toSave : t)
    } else {
      customThemes.value.push(toSave)
    }
    localStorage.setItem(STORAGE_KEYS.custom, JSON.stringify(customThemes.value))
    activeThemeId.value = toSave.id
    localStorage.setItem(STORAGE_KEYS.active, toSave.id)
  }

  applyThemeToDOM(editingTheme.value.colors)
  isEditing.value = false
  editingTheme.value = null
}

const deleteTheme = (id, isDefault) => {
  if (isDefault) {
    defaultThemes.value = defaultThemes.value.filter(t => t.id !== id)
    localStorage.setItem(STORAGE_KEYS.defaults, JSON.stringify(defaultThemes.value))
  } else {
    customThemes.value = customThemes.value.filter(t => t.id !== id)
    localStorage.setItem(STORAGE_KEYS.custom, JSON.stringify(customThemes.value))
  }
  if (activeThemeId.value === id) {
    const fallback = defaultThemes.value[0]?.id || customThemes.value[0]?.id || 'docfusion'
    applyThemeById(fallback)
  }
}

const resetDefaults = () => {
  if (!confirm('确定要重置所有预设主题到初始状态？')) return
  defaultThemes.value = [...INITIAL_DEFAULTS]
  localStorage.removeItem(STORAGE_KEYS.defaults)
  applyThemeById('docfusion')
}

onMounted(() => {
  try {
    const saved = localStorage.getItem(STORAGE_KEYS.custom)
    if (saved) customThemes.value = JSON.parse(saved)
  } catch {}
  try {
    const saved = localStorage.getItem(STORAGE_KEYS.defaults)
    if (saved) defaultThemes.value = JSON.parse(saved)
  } catch {}
  const savedId = localStorage.getItem(STORAGE_KEYS.active)
  if (savedId) {
    activeThemeId.value = savedId
  }
  applyThemeToDOM(findTheme(activeThemeId.value).colors)
})
</script>

<style scoped>
.theme-selector-wrapper {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 2000;
}

/* Floating Action Button */
.tp-fab {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.5);
  background: rgba(255, 255, 255, 0.8);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: #666;
  transition: all 0.25s;
}
.tp-fab:hover { transform: scale(1.08); color: #333; box-shadow: 0 6px 28px rgba(0, 0, 0, 0.15); }
.tp-fab.open { background: var(--primary-color, #5b6abf); color: #fff; border-color: transparent; }

/* Panel */
.theme-panel {
  position: absolute;
  bottom: 60px;
  right: 0;
  width: 320px;
  max-height: 80vh;
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border: 1px solid rgba(255, 255, 255, 0.5);
  border-radius: 16px;
  padding: 16px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.12);
}

/* Panel transition */
.theme-pop-enter-active { transition: all 0.25s cubic-bezier(0.34, 1.56, 0.64, 1); }
.theme-pop-leave-active { transition: all 0.18s ease-in; }
.theme-pop-enter-from, .theme-pop-leave-to { opacity: 0; transform: scale(0.9) translateY(12px); }

/* Header */
.tp-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; padding: 0 2px; }
.tp-header h3 { font-size: 13px; font-weight: 600; color: #555; }
.tp-actions { display: flex; gap: 4px; }

.tp-icon-btn {
  width: 28px; height: 28px; border-radius: 8px; border: none;
  background: #f0f0f0; color: #666; display: flex; align-items: center; justify-content: center;
  cursor: pointer; transition: all 0.2s;
}
.tp-icon-btn:hover { background: #e0e0e0; color: #333; }

/* Section */
.tp-section { margin-bottom: 12px; }
.tp-section h4 { font-size: 11px; font-weight: 500; color: #999; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; padding: 0 2px; }

/* Theme item */
.tp-list { display: flex; flex-direction: column; gap: 4px; }
.tp-item {
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  border-radius: 10px; cursor: pointer; transition: all 0.2s; position: relative;
}
.tp-item:hover { background: rgba(255, 255, 255, 0.5); }
.tp-item.active { background: #fff; box-shadow: 0 1px 6px rgba(0, 0, 0, 0.06); }

.tp-swatch { width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0; border: 1px solid rgba(0, 0, 0, 0.06); box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.08); }
.tp-name { flex: 1; font-size: 13px; font-weight: 500; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tp-check { color: #555; font-size: 14px; flex-shrink: 0; }

.tp-tiny-btn {
  width: 22px; height: 22px; border-radius: 6px; border: none;
  background: transparent; color: #bbb; display: flex; align-items: center; justify-content: center;
  cursor: pointer; opacity: 0; transition: all 0.2s; flex-shrink: 0; font-size: 12px;
}
.tp-item:hover .tp-tiny-btn { opacity: 1; }
.tp-tiny-btn:hover { color: #2F81F7; }
.tp-tiny-btn.danger:hover { color: #e07070; }

/* Edit tabs */
.tp-tabs { display: flex; gap: 6px; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid #eee; }
.tp-tabs button {
  font-size: 12px; font-weight: 500; padding: 4px 10px; border-radius: 6px;
  border: none; cursor: pointer; background: transparent; color: #999; transition: all 0.2s;
}
.tp-tabs button.active { background: #222; color: #fff; }
.tp-tabs button:not(.active):hover { background: #f0f0f0; }

/* Edit fields */
.tp-edit-body { display: flex; flex-direction: column; gap: 14px; }
.tp-field label { display: block; font-size: 11px; color: #999; margin-bottom: 6px; }
.tp-input {
  width: 100%; padding: 7px 10px; border-radius: 8px;
  border: 1px solid #e0e0e0; background: rgba(255, 255, 255, 0.5);
  font-size: 13px; outline: none; transition: border-color 0.2s;
}
.tp-input:focus { border-color: var(--primary-color, #5b6abf); }
.tp-input.mono, .tp-textarea.mono { font-family: 'SF Mono', 'Consolas', monospace; font-size: 12px; }
.tp-textarea {
  width: 100%; padding: 7px 10px; border-radius: 8px;
  border: 1px solid #e0e0e0; background: rgba(255, 255, 255, 0.5);
  font-size: 13px; outline: none; resize: none; transition: border-color 0.2s;
}
.tp-textarea:focus { border-color: var(--primary-color, #5b6abf); }

.tp-color-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.tp-color-item {
  display: flex; align-items: center; gap: 8px; padding: 6px 8px;
  background: rgba(255, 255, 255, 0.5); border-radius: 8px; border: 1px solid #f0f0f0;
}
.tp-color-item input[type="color"] { width: 24px; height: 24px; border: none; border-radius: 4px; cursor: pointer; padding: 0; background: transparent; }
.tp-color-item span { font-size: 12px; color: #666; }

.tp-gradient-row { display: flex; gap: 10px; justify-content: center; }
.tp-gradient-item { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.tp-color-round { width: 32px; height: 32px; border-radius: 50%; border: 2px solid #fff; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.1); cursor: pointer; padding: 0; }
.tp-gradient-item span { font-size: 10px; color: #bbb; }

.tp-hint { padding: 10px; background: #f8f8f8; border-radius: 8px; font-size: 12px; color: #999; line-height: 1.5; }

.tp-palette-list { display: flex; flex-direction: column; gap: 8px; }
.tp-palette-item {
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  background: rgba(255, 255, 255, 0.5); border-radius: 8px; border: 1px solid #f0f0f0;
}
.tp-palette-label { font-size: 12px; color: #bbb; width: 20px; }
.tp-palette-item input[type="color"] { width: 28px; height: 28px; border: none; border-radius: 4px; cursor: pointer; padding: 0; background: transparent; }
.tp-palette-bar { flex: 1; height: 8px; border-radius: 4px; }

/* Save button */
.tp-save-btn {
  width: 100%; padding: 10px; margin-top: 14px; border: none; border-radius: 10px;
  background: #222; color: #fff; font-size: 13px; font-weight: 500;
  display: flex; align-items: center; justify-content: center; gap: 6px;
  cursor: pointer; transition: background 0.2s;
}
.tp-save-btn:hover { background: #333; }

/* Scrollbar inside panel */
.theme-panel::-webkit-scrollbar { width: 4px; }
.theme-panel::-webkit-scrollbar-thumb { background: #ddd; border-radius: 2px; }

/* Cursor toggle row */
.tp-cursor-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 10px 2px;
  border-top: 1px solid #f0f0f0;
  margin-top: 8px;
}
.tp-cursor-label {
  font-size: 12px; color: #888;
  display: flex; align-items: center;
}
.tp-cursor-toggle {
  width: 40px; height: 22px;
  border-radius: 11px;
  border: none; cursor: pointer;
  background: #ddd;
  position: relative;
  transition: background 0.25s;
  flex-shrink: 0;
  padding: 0;
}
.tp-cursor-toggle.on { background: var(--primary-color, #5b6abf); }
.tp-cursor-thumb {
  position: absolute;
  top: 3px; left: 3px;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
  transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1);
  display: block;
}
.tp-cursor-toggle.on .tp-cursor-thumb { transform: translateX(18px); }
</style>
