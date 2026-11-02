import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as Icons from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import './assets/global.css'

const app = createApp(App)

app.use(ElementPlus)
app.use(router)

// 全量注册 Element Plus 图标，模板里可直接 <el-icon><Search /></el-icon>
for (const [name, component] of Object.entries(Icons)) {
  app.component(name, component)
}

app.mount('#app')
