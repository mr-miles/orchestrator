/** File lists as folder trees, for the file browsers. */
export interface TreeFile {
  name: string
  path: string
  note?: string
}

export interface TreeDir {
  name: string
  path: string
  dirs: TreeDir[]
  files: TreeFile[]
}

/** A folder tree from paths (folders first, both sorted by name), and every folder's path. */
export function buildTree(files: { path: string; note?: string }[]): {
  root: TreeDir
  allDirs: string[]
} {
  const root: TreeDir = { name: '', path: '', dirs: [], files: [] }
  const index = new Map<string, TreeDir>([['', root]])
  for (const f of files) {
    const parts = f.path.split('/').filter(Boolean)
    let dir = root
    for (let i = 0; i < parts.length - 1; i++) {
      const path = parts.slice(0, i + 1).join('/')
      let next = index.get(path)
      if (!next) {
        next = { name: parts[i] ?? '', path, dirs: [], files: [] }
        index.set(path, next)
        dir.dirs.push(next)
      }
      dir = next
    }
    dir.files.push({ name: parts[parts.length - 1] ?? f.path, path: f.path, note: f.note })
  }
  const sort = (d: TreeDir) => {
    d.dirs.sort((a, b) => a.name.localeCompare(b.name))
    d.files.sort((a, b) => a.name.localeCompare(b.name))
    d.dirs.forEach(sort)
  }
  sort(root)
  return { root, allDirs: [...index.keys()].filter(Boolean) }
}

export function formatSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / 1024 / 1024).toFixed(1)} MiB`
}
