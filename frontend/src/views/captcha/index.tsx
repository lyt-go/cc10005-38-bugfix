import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/captcha'
const columns = [
  "策略编号",
  "验证码类型",
  "识别服务商",
  "识别超时秒",
  "重试次数上限",
  "降级策略",
  "手动打码开关",
  "打码等待队列"
]
const extraColumns = ["当前识别出口", "最近识别结果"]
const actions = [
  "提交策略验证",
  "启用策略",
  "降级策略"
]
const statuses = [
  "草稿",
  "策略验证中",
  "已启用",
  "已降级"
]
const statLabels = ["验证码策略总数", "已启用策略", "验证中策略"]

type RecognitionRecord = {
  结果编号?: string
  代理实例编号?: string
  代理版本?: number
  状态?: string
  撤销原因?: string | null
}

type Row = {
  id: number | string
  status?: string
  当前分配代理?: string | null
  当前分配代理版本?: number | null
  识别记录?: RecognitionRecord[]
  [key: string]: unknown
}

/** 各状态下可执行的动作（仅作按钮置灰提示，最终以后端拦截为准） */
function allowedActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交策略验证']
    case '策略验证中':
      // 识别出口被回收撤销后允许重新提交以恢复验证；出口有效时重复提交会被后端拦下
      return row.当前分配代理 ? ['启用策略'] : ['提交策略验证', '启用策略']
    case '已启用':
      return ['降级策略']
    case '已降级':
      return ['提交策略验证']
    default:
      return []
  }
}

function lastRecognition(row: Row): string {
  const records = row.识别记录 ?? []
  if (records.length === 0) return '—'
  const last = records[records.length - 1]
  const proxy = last.代理实例编号 ? `${last.代理实例编号} v${last.代理版本 ?? '—'}` : ''
  return [last.状态 ?? '—', proxy].filter(Boolean).join(' · ')
}

export default function CaptchaView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<Record<string, number>>({})
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('验证码策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setStats(payload.stats ?? {})
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '验证码处理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || payload?.ok === false) {
        throw new Error(payload?.message ?? '验证码处理动作未生效，请稍后重试')
      }
      setNotice(payload?.message ?? `已执行：${action}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '验证码处理操作失败')
    }
  }

  const exportQuery = new URLSearchParams(filters).toString()

  return (
    <section className="page" data-module="captcha">
      <header className="page-head">
        <div>
          <h2>验证码处理管理</h2>
          <p className="page-desc">管理各类型验证码的自动识别与人工打码策略，包括图像验证码、滑块和点击验证。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('验证码策略登记入口尚未接入审批流')}>
            登记验证码策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export?${exportQuery}`, '_blank')}>
            导出验证码处理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statLabels.map((label) => (
          <article key={label} className="stat-card">
            <span className="stat-label">{label}</span>
            <strong className="stat-value">{stats[label] ?? 0}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            {extraColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const enabled = allowedActions(row)
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
                <td>{row.当前分配代理 ? `${row.当前分配代理}（v${row.当前分配代理版本 ?? '—'}）` : '—'}</td>
                <td>{lastRecognition(row)}</td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={!enabled.includes(action)}
                      title={enabled.includes(action) ? undefined : `当前状态「${row.status}」不可执行该动作`}
                      onClick={() => void runAction(action, row)}
                    >
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + extraColumns.length + 1}>暂无验证码处理数据，可先登记验证码策略</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条验证码处理记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
