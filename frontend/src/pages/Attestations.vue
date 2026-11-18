<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api.js'

const route = useRoute()
const rows = ref([])
const overview = ref(null)
const err = ref('')
const loading = ref(false)

const focus = computed(() => route.query.wb || '')

async function load() {
  loading.value = true
  err.value = ''
  try {
    const [att, ov] = await Promise.all([
      api.attestations(focus.value || undefined),
      api.overview(),
    ])
    rows.value = att.items
    overview.value = ov
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(focus, load)

function kindLabel(r) {
  return r.bypass_rejected ? '旁路拒绝' : r.kind === 'derived' ? '同事务重算' : r.kind
}
</script>

<template>
  <div class="page-card">
    <div class="page-head">
      <h1>投影同源核证</h1>
      <button class="btn ghost" :disabled="loading" @click="load">刷新</button>
    </div>
    <p class="lead">
      只读核证实体：每条记录绑定末有效 hop、映射码、投影值与「是否旁路拒绝」标志。
      <code>derived</code> 核证由投影服务在正式时间线成功变更的同一事务内写入，
      与时间线同成败；<code>bypass_rejected</code> 核证对应被整笔回滚的直接改投影请求。
    </p>
    <p v-if="err" class="muted">{{ err }}</p>

    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.projection_attestations }}</b><span>核证总数</span></div>
      <div class="stat" :class="{ bad: overview.bypass_rejections }">
        <b>{{ overview.bypass_rejections }}</b><span>旁路拒绝</span>
      </div>
      <div class="stat ok"><b>{{ overview.projection_aligned }}</b><span>投影同源</span></div>
      <div class="stat" :class="{ bad: overview.projection_diverged }">
        <b>{{ overview.projection_diverged }}</b><span>投影分叉</span>
      </div>
    </div>

    <table class="table">
      <thead>
        <tr>
          <th>id</th><th>运单</th><th>类型</th><th>末 hop</th><th>映射码</th>
          <th>投影值</th><th>旁路尝试值</th><th>时间线摘要</th><th>时刻</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id" :class="{ rowbad: r.bypass_rejected }">
          <td>{{ r.id }}</td>
          <td><router-link :to="`/attestations?wb=${r.waybill_id}`">{{ r.waybill_id }}</router-link></td>
          <td>
            <span class="tag" :class="r.bypass_rejected ? 'bad' : 'ok'">{{ kindLabel(r) }}</span>
          </td>
          <td>{{ r.last_hop_id || '—' }}</td>
          <td>{{ r.mapped_code || '—' }}</td>
          <td>{{ r.proj_value || '—' }}</td>
          <td :title="r.reason || ''">{{ r.attempted_value || '—' }}</td>
          <td :title="r.timeline_digest || ''">{{ (r.timeline_digest || '').slice(0, 12) }}…</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="9" class="muted">暂无核证记录</td></tr>
      </tbody>
    </table>

    <p class="hint" v-if="focus">
      当前按运单 <b>{{ focus }}</b> 过滤 ·
      <router-link to="/attestations" class="inline-link">查看全部</router-link>
    </p>
  </div>
</template>
