<template>
  <section class="page" data-module="runtime">
    <header class="page-head">
      <div>
        <h2>运行实例管理</h2>
        <p class="page-desc">记录每次ETL作业的运行实例，追踪执行状态和数据吞吐量。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记运行记录</button>
        <button class="btn" type="button" @click="exportRows">导出运行实例清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="page-desc" v-if="snapshotMeta">
      类型化快照版本 v{{ snapshotMeta.snapshot_version }}（来源 {{ snapshotMeta.source_version }}）：
      状态、时间、吞吐与基线计算同一口径；缺失结束时间 {{ snapshotMeta.missing_end_time }} 条、
      非法吞吐 {{ snapshotMeta.invalid_throughput }} 条、状态冲突 {{ snapshotMeta.quality.conflict_records }} 条。
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>类型化状态</th>
          <th>类型化开始时间</th>
          <th>类型化结束时间</th>
          <th>类型化读取行数</th>
          <th>类型化写入行数</th>
          <th>快照/来源/数据版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td>
            <span :title="markerTitle(row)">{{ row.typed_status ?? '—' }}</span>
            <em v-if="row.compatibility_markers?.['执行状态'] && row.compatibility_markers['执行状态'] !== 'ok'" class="marker">
              （{{ row.compatibility_markers['执行状态'] }}）
            </em>
          </td>
          <td>{{ row.typed_start_time ?? '—' }}</td>
          <td>{{ row.typed_end_time ?? '—' }}</td>
          <td>{{ row.typed_read_rows ?? '—' }}</td>
          <td>{{ row.typed_write_rows ?? '—' }}</td>
          <td>v{{ row.snapshot_version ?? '—' }} / {{ row.source_version ?? '—' }} / 数据版本{{ row.data_version ?? '—' }}</td>
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
          <td :colspan="columns.length + 7" class="empty-state">暂无运行实例数据，可先登记运行记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条运行实例记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request, fetchJson } from '@/api/client'

type Row = Record<string, any>
type SnapshotMeta = {
  snapshot_version: number
  source_version: string
  total: number
  missing_end_time: number
  invalid_throughput: number
  status_counts: Record<string, number>
  quality: { conflict_records: number; invalid_records: number; missing_records: number }
}

const ENDPOINT = '/api/runtime'
const columns = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "读取行数", "写入行数", "错误信息"]
const actions = ["启动作业", "终止作业", "标记成功"]
const statuses = ["草稿", "运行中", "成功", "失败", "已终止"]
const MARKER_TEXT: Record<string, string> = {
  ok: '可解析',
  migrated: '旧格式已映射',
  missing: '历史缺失',
  invalid: '历史非法',
  conflict: '新旧值冲突，以类型化值为准',
}
const stats = ref([
  { label: "运行记录总数", value: 0 },
  { label: "成功记录数", value: 0 },
  { label: "非法吞吐记录", value: 0 },
])
const snapshotMeta = ref<SnapshotMeta | null>(null)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function formatCell(row: Row, column: string): string | number | null {
  const value = row[column]
  return value === '' || value === null || value === undefined ? '—' : value
}

function markerTitle(row: Row): string {
  const markers = row.compatibility_markers as Record<string, string> | undefined
  if (!markers) return ''
  return Object.entries(markers)
    .filter(([, marker]) => marker !== 'ok')
    .map(([field, marker]) => `${field}：${MARKER_TEXT[marker] ?? marker}`)
    .join('；')
}

async function loadStats() {
  try {
    const payload = await fetchJson<SnapshotMeta>(`${ENDPOINT}/stats`)
    snapshotMeta.value = payload
    stats.value = [
      { label: "运行记录总数", value: payload.total as number },
      { label: "成功记录数", value: payload.status_counts['成功'] as number },
      { label: "非法吞吐记录", value: payload.invalid_throughput as number },
    ]
  } catch {
    // 统计失败不阻塞清单读取。
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '运行记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('运行实例动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('运行记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.marker {
  color: #b45309;
  font-style: normal;
  font-size: 12px;
}
</style>
