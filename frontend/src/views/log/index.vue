<template>
  <section class="page" data-module="log">
    <header class="page-head">
      <div>
        <h2>采集日志管理</h2>
        <p class="page-desc">维护采集任务的执行日志、数据量与异常记录，提供采集全链路可追溯。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记执行日志</button>
        <button class="btn" type="button" :disabled="retrying" @click="retrySelected">
          {{ retrying ? '批量重试中…' : '批量重试选中' }}
        </button>
        <button class="btn" type="button" @click="exportRows">导出采集日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div v-if="retryResults.length" class="retry-result">
      <p
        v-for="item in retryResults"
        :key="String(item.id)"
        :class="item.ok ? 'ok-line' : 'fail-line'"
      >
        {{ item.日志编号 || `#${item.id}` }}：{{ item.message }}
      </p>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th class="check-col">
            <input type="checkbox" :checked="allSelected" @change="toggleAll" />
          </th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td class="check-col">
            <input v-model="selectedIds" type="checkbox" :value="row.id" />
          </td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集日志数据，可先登记执行日志</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集日志记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type RetryResult = { id: number | string; 日志编号: string; ok: boolean; message: string }

const ENDPOINT = '/api/log'
const columns = ["日志编号", "关联任务", "开始时间", "结束时间", "采集行数", "成功条数", "失败条数", "错误信息", "断点批次", "来源日志编号"]
const actions = ["标记已读", "触发重试", "归档日志"]
const statuses = ["成功", "部分失败", "完全失败"]
const stats = [{"label": "执行日志总数", "value": 0}, {"label": "成功日志", "value": 0}, {"label": "失败日志", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const selectedIds = ref<Array<string | number>>([])
const retryResults = ref<RetryResult[]>([])
const retrying = ref(false)

const allSelected = computed(
  () => rows.value.length > 0 && rows.value.every((row) => selectedIds.value.includes(row.id as string | number)),
)

function toggleAll() {
  selectedIds.value = allSelected.value ? [] : rows.value.map((row) => row.id as string | number)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '执行日志登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '采集日志动作未生效，请稍后重试')
    }
    noticeMessage.value = String(payload.message || '')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集日志操作失败'
  }
}

async function retrySelected() {
  if (!selectedIds.value.length) {
    errorMessage.value = '请先勾选要重试的执行日志'
    return
  }
  retrying.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/retry-batch`, {
      method: 'POST',
      body: JSON.stringify({ ids: selectedIds.value }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload) {
      throw new Error(payload?.message || '批量重试请求失败')
    }
    retryResults.value = payload.results ?? []
    noticeMessage.value = String(payload.message || '')
    selectedIds.value = []
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量重试失败'
  } finally {
    retrying.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('执行日志列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集日志列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.check-col { width: 32px; text-align: center; }
.retry-result { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin-bottom: 12px; font-size: 13px; }
.retry-result p { margin: 4px 0; }
.ok-line { color: #067647; }
.fail-line { color: #b42318; }
.notice-text { color: #067647; }
</style>
