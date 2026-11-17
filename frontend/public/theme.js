// Applies the saved colour theme before the first paint (a same-origin script: the controller's
// CSP allows no inline script). The ThemeProvider keeps it in sync afterwards.
;(() => {
  const saved = localStorage.getItem('legion-theme') || 'system'
  const dark =
    saved === 'dark' || (saved === 'system' && matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.add(dark ? 'dark' : 'light')
})()
