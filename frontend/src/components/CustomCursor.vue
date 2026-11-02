<template>
  <div v-if="enabled" class="custom-cursor" :style="cursorStyle" :class="{ hovering: isHovering }"></div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

const x = ref(0)
const y = ref(0)
const isHovering = ref(false)
const enabled = ref(localStorage.getItem('docfusion_custom_cursor') !== 'false')

const cursorStyle = computed(() => ({
  transform: 'translate(' + (x.value - 16) + 'px, ' + (y.value - 16) + 'px)',
}))

const onMouseMove = e => {
  x.value = e.clientX
  y.value = e.clientY
  const target = e.target
  isHovering.value = !!(
    target.closest('button') ||
    target.closest('a') ||
    target.closest('.clickable') ||
    target.closest('.el-button') ||
    target.closest('.nav-item') ||
    target.closest('.theme-item')
  )
}

const onToggle = e => {
  enabled.value = e.detail
}

onMounted(() => {
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('cursor-toggle', onToggle)
})
onBeforeUnmount(() => {
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('cursor-toggle', onToggle)
})
</script>

<style scoped>
.custom-cursor {
  position: fixed;
  top: 0;
  left: 0;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: 2px solid var(--primary-color);
  pointer-events: none;
  z-index: 9999;
  transition:
    transform 0.12s cubic-bezier(0.25, 0.8, 0.25, 1),
    width 0.2s,
    height 0.2s,
    background 0.2s,
    opacity 0.2s;
  opacity: 0.7;
}
.custom-cursor.hovering {
  width: 44px;
  height: 44px;
  background: var(--primary-color);
  opacity: 0.2;
  margin-left: -6px;
  margin-top: -6px;
}
@media (pointer: coarse) {
  .custom-cursor {
    display: none;
  }
}
</style>
