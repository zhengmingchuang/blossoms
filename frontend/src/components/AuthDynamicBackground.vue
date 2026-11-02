<template>
  <div class="auth-bg-canvas-wrapper">
    <canvas ref="canvasRef"></canvas>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'

const canvasRef = ref(null)
let ctx = null
let shapes = []
let currentCount = 0
let animationId = null
let resizeObserver = null
let width = 0
let height = 0
const mouse = { x: 0, y: 0, active: false }

const maxElements = 70
const explosionRadius = 180
const explosionForce = 3.5
// DocFusion 主色调：蓝紫
const baseHue = 230
const hueRange = 35

const rand = (min, max) => min + Math.random() * (max - min)
const pickType = () => {
  const r = Math.random()
  return r < 0.35 ? 'circle' : r < 0.65 ? 'square' : 'triangle'
}

const createShape = () => {
  const size = rand(8, 44)
  return {
    x: Math.random() * width,
    y: Math.random() * height,
    vx: Math.cos(Math.random() * Math.PI * 2) * rand(0.15, 0.5),
    vy: Math.sin(Math.random() * Math.PI * 2) * rand(0.15, 0.5),
    size,
    type: pickType(),
    rotation: Math.random() * Math.PI * 2,
    rotationSpeed: rand(-0.008, 0.008),
    hue: baseHue + rand(-hueRange, hueRange),
    opacity: rand(0.25, 0.7),
    boost: 0,
  }
}

const initShapes = () => {
  shapes = []
  for (let i = 0; i < maxElements; i++) shapes.push(createShape())
  currentCount = Math.min(maxElements, window.innerWidth < 768 ? 35 : 65)
}

const resize = () => {
  const c = canvasRef.value
  if (!c) return
  const dpr = window.devicePixelRatio || 1
  width = c.clientWidth
  height = c.clientHeight
  c.width = width * dpr
  c.height = height * dpr
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
}

const applyMouse = s => {
  if (!mouse.active) return
  const dx = mouse.x - s.x,
    dy = mouse.y - s.y
  const distSq = dx * dx + dy * dy
  const maxD = 300,
    maxDSq = maxD * maxD
  if (distSq > maxDSq || distSq === 0) return
  const dist = Math.sqrt(distSq)
  const force = (1 - dist / maxD) * 0.16
  s.vx += (dx / dist) * force
  s.vy += (dy / dist) * force
}

const applyExplosion = (x, y) => {
  const r = explosionRadius * 1.5,
    rSq = r * r
  for (let i = 0; i < currentCount; i++) {
    const s = shapes[i]
    const dx = s.x - x,
      dy = s.y - y,
      dSq = dx * dx + dy * dy
    if (dSq > rSq || dSq === 0) continue
    const d = Math.sqrt(dSq),
      n = 1 - d / r
    const impulse = explosionForce * Math.pow(n, 0.4) * 5
    s.vx += (dx / d) * impulse
    s.vy += (dy / d) * impulse
    s.boost = 1
  }
}

const update = s => {
  applyMouse(s)
  s.vx += rand(-0.006, 0.006)
  s.vy += rand(-0.006, 0.006)
  const maxSpd = 2 + (s.boost > 0 ? 3 * s.boost : 0)
  const spd = Math.sqrt(s.vx * s.vx + s.vy * s.vy)
  if (spd > maxSpd) {
    const sc = maxSpd / spd
    s.vx *= sc
    s.vy *= sc
  }
  s.x += s.vx
  s.y += s.vy
  const h = s.size / 2
  if (s.x - h < 0) {
    s.x = h
    if (s.vx < 0) s.vx *= -0.8
  } else if (s.x + h > width) {
    s.x = width - h
    if (s.vx > 0) s.vx *= -0.8
  }
  if (s.y - h < 0) {
    s.y = h
    if (s.vy < 0) s.vy *= -0.8
  } else if (s.y + h > height) {
    s.y = height - h
    if (s.vy > 0) s.vy *= -0.8
  }
  s.vx *= 0.986
  s.vy *= 0.986
  s.rotation += s.rotationSpeed
  if (s.boost > 0) {
    s.boost *= 0.9
    if (s.boost < 0.04) s.boost = 0
  }
}

const draw = s => {
  ctx.save()
  ctx.translate(s.x, s.y)
  ctx.rotate(s.rotation)
  ctx.strokeStyle = `hsla(${s.hue}, 65%, 58%, ${s.opacity})`
  ctx.lineWidth = 1.2
  const sz = s.size,
    h = sz / 2
  ctx.beginPath()
  if (s.type === 'circle') {
    ctx.arc(0, 0, h, 0, Math.PI * 2)
  } else if (s.type === 'square') {
    ctx.rect(-h, -h, sz, sz)
  } else {
    ctx.moveTo(0, -h)
    ctx.lineTo(h, h)
    ctx.lineTo(-h, h)
    ctx.closePath()
  }
  ctx.stroke()
  ctx.restore()
}

const loop = () => {
  if (!ctx) return
  ctx.clearRect(0, 0, width, height)
  // 柔和的双色渐变底色
  const grad = ctx.createLinearGradient(0, 0, width, height)
  grad.addColorStop(0, '#eef1fb')
  grad.addColorStop(0.5, '#f0f4f8')
  grad.addColorStop(1, '#e8f0ee')
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, width, height)
  for (let i = 0; i < currentCount; i++) {
    update(shapes[i])
    draw(shapes[i])
  }
  animationId = requestAnimationFrame(loop)
}

const onPointerMove = e => {
  const c = canvasRef.value
  if (!c) return
  const r = c.getBoundingClientRect()
  const x = e.clientX - r.left,
    y = e.clientY - r.top
  if (x < 0 || x > r.width || y < 0 || y > r.height) {
    mouse.active = false
    return
  }
  mouse.x = x
  mouse.y = y
  mouse.active = true
}
const onClick = e => {
  const c = canvasRef.value
  if (!c) return
  const r = c.getBoundingClientRect()
  const x = e.clientX - r.left,
    y = e.clientY - r.top
  if (x >= 0 && x <= r.width && y >= 0 && y <= r.height) applyExplosion(x, y)
}
const onTouchMove = e => {
  if (!e.touches?.length) return
  const t = e.touches[0],
    c = canvasRef.value
  if (!c) return
  const r = c.getBoundingClientRect()
  const x = t.clientX - r.left,
    y = t.clientY - r.top
  if (x < 0 || x > r.width || y < 0 || y > r.height) {
    mouse.active = false
    return
  }
  mouse.x = x
  mouse.y = y
  mouse.active = true
}
const onTouchStart = e => {
  if (!e.touches?.length) return
  const t = e.touches[0],
    c = canvasRef.value
  if (!c) return
  const r = c.getBoundingClientRect()
  const x = t.clientX - r.left,
    y = t.clientY - r.top
  if (x >= 0 && x <= r.width && y >= 0 && y <= r.height) applyExplosion(x, y)
}
const onTouchEnd = () => {
  mouse.active = false
}

onMounted(() => {
  const c = canvasRef.value
  if (!c) return
  ctx = c.getContext('2d')
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(c)
  resize()
  initShapes()
  animationId = requestAnimationFrame(loop)
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('click', onClick)
  window.addEventListener('touchstart', onTouchStart, { passive: true })
  window.addEventListener('touchmove', onTouchMove, { passive: true })
  window.addEventListener('touchend', onTouchEnd)
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  if (animationId) cancelAnimationFrame(animationId)
  if (resizeObserver && canvasRef.value) resizeObserver.unobserve(canvasRef.value)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('click', onClick)
  window.removeEventListener('touchstart', onTouchStart)
  window.removeEventListener('touchmove', onTouchMove)
  window.removeEventListener('touchend', onTouchEnd)
  window.removeEventListener('resize', resize)
})
</script>

<style scoped>
.auth-bg-canvas-wrapper {
  position: absolute;
  inset: 0;
  z-index: 0;
}
.auth-bg-canvas-wrapper canvas {
  width: 100%;
  height: 100%;
  display: block;
}
</style>
