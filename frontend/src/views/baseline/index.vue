<template>
  <section class="page" data-module="baseline">
    <header class="page-head">
      <div>
        <h2>基线管理管理</h2>
        <p class="page-desc">管理ETL任务的产出基线，定义承诺完成时间和预警机制。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记基线定义</button>
        <button class="btn" type="button" @click="recalculate">按运行快照重算</button>
        <button class="btn" type="button" @click="exportRows">导出基线管理清单</button>
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

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>关联运行数</th>
          <th>成功完成数</th>
          <th>准时/破线</th>
          <th>读取行数合计</th>
          <th>质量（缺失/非法/冲突）</th>
          <th>快照版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['运行实例结果']?.['关联运行数'] ?? '—' }}</td>
          <td>{{ row['运行实例结果']?.['成功完成数'] ?? '—' }}</td>
          <td>{{ row['运行实例结果']?.['准时完成数'] ?? '—' }} / {{ row['运行实例结果']?.['破线数'] ?? '—' }}</td>
          <td>{{ row['运行实例结果']?.['读取行数合计'] ?? '—' }}</td>
          <td>
            {{ row['运行实例结果']?.['缺失结束时间数'] ?? 0 }} /
            {{ row['运行实例结果']?.['非法吞吐数'] ?? 0 }} /
            {{ row['运行实例结果']?.['状态冲突数'] ?? 0 }}
          </td>
          <td>v{{ row['运行实例结果']?.['快照版本'] ?? '—' }}</td>
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
          <td :colspan="columns.length + 8" class="empty-state">暂无基线管理数据，可先登记基线定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条基线管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/baseline'
const columns = ["基线编号", "基线名称", "承诺完成时间", "预警余量分钟", "破线级别", "关联作业列表", "工作日标记", "节假日偏移"]
const actions = ["提交基线评审", "启用基线", "停用基线"]
const statuses = ["草稿", "基线评审中", "已启用", "已停用"]
const stats = [{"label": "基线定义总数", "value": 0}, {"label": "已启用基线", "value": 0}, {"label": "评审中基线", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '基线定义登记入口尚未接入审批流'
}

async function recalculate() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/recalculate`, { method: 'POST' })
    if (!response.ok) {
      throw new Error('基线重算未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线重算失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('基线管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('基线定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线管理列表读取失败'
  }
}

onMounted(reload)
</script>
