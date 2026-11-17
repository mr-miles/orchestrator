import { GlobalRegistrator } from '@happy-dom/global-registrator'

GlobalRegistrator.register()

// React Testing Library: state updates are wrapped in act()
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
