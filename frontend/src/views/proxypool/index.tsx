import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/proxypool'
const columns = [
  "实例编号",
  "代理地址",
  "代理端口",
  "匿名等级",
  "出站地域",
  "响应毫秒",
  "存活检测时间",
  "分配权重"
]
const extraColumns = ["代理版本", "当前分配策略", "冻结出站地域", "冻结响应毫秒"]
const actions = [
  "提交存活检测",
  "标记可用",
  "回收实例"
]
const statuses = [
  "草稿",
  "存活检测中",
  "可用",
  "已回收"
]
const statLabels = ["代理实例总数", "可用实例数", "检测中实例数"]

type Row = {
  id: number | string
  status?: string
  代理版本?: number | null
  当前分配策略?: string | null
  冻结出站地域?: string | null
  冻结响应毫秒?: string | null
  [key: string]: unknown
}

/** 各状态下可执行的动作（仅作按钮置灰提示，最终以后端拦截为准） */
function allowedActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交存活检测', '回收实例']
    case '存活检测中':
      return ['标记可用', '回收实例']
    case '可用':
      return ['回收实例']
    default:
      // 已回收为终态，不再提供任何动作
      return []
  }
}

export default function ProxypoolView() {
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
        throw new Error('代理实例列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setStats(payload.stats ?? {})
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '代理池管理列表读取失败')
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
        throw new Error(payload?.message ?? '代理池管理动作未生效，请稍后重试')
      }
      setNotice(payload?.message ?? `已执行：${action}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '代理池管理操作失败')
    }
  }

  const exportQuery = new URLSearchParams(filters).toString()

  return (
    <section className="page" data-module="proxypool">
      <header className="page-head">
        <div>
          <h2>代理池管理管理</h2>
          <p className="page-desc">管理动态代理池，维护代理的可用性检测、地域分布和负载均衡调度。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('代理实例登记入口尚未接入审批流')}>
            登记代理实例
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export?${exportQuery}`, '_blank')}>
            导出代理池管理清单
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
                <td>{row.代理版本 ?? '—'}</td>
                <td>{row.当前分配策略 ?? '—'}</td>
                <td>{row.冻结出站地域 ?? '—'}</td>
                <td>{row.冻结响应毫秒 ?? '—'}</td>
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
              <td className="empty-state" colSpan={columns.length + extraColumns.length + 1}>暂无代理池管理数据，可先登记代理实例</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条代理池管理记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
