import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'
import prettier from 'eslint-config-prettier'
import globals from 'globals'

/**
 * ESLint 扁平配置（ESLint 9+ / 10）。
 *
 * 分层顺序：JS 基础规则 → TS 规则 → Vue 规则 → 项目自定义 → 关闭与 Prettier 冲突的格式规则。
 * 格式化交给 Prettier，ESLint 只负责代码质量，两者不重叠。
 */
export default [
  {
    ignores: ['dist/**', 'node_modules/**', 'public/**', 'coverage/**', '*.min.js'],
  },

  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],

  {
    files: ['**/*.{ts,vue,js}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      // 页面组件（Dashboard / Login …）是单词名，Vue 官方也允许，这里关掉
      'vue/multi-word-component-names': 'off',
      // 模板换行由 Prettier 决定，避免两套规则打架
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/first-attribute-linebreak': 'off',
      // 后端返回体字段多为下划线命名，对齐接口比驼峰更可读
      camelcase: 'off',
      // 未使用变量降级为提醒：视图层常有预留的解构字段
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // 允许 `catch {}` 空块（SSE 半包解析里是刻意为之）
      'no-empty': ['error', { allowEmptyCatch: true }],
      'no-console': ['warn', { allow: ['warn', 'error'] }],
    },
  },

  // 配置文件本身跑在 Node 里
  {
    files: ['*.config.js', 'vite.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },

  // 必须放最后：关闭所有与 Prettier 冲突的格式类规则
  prettier,
]
