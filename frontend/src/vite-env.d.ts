/// <reference types="vite/client" />

/**
 * 让 TypeScript 认识 .vue 单文件组件。
 * 视图层目前仍是 JS，这里只声明模块形状，不做 props 级类型检查。
 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
