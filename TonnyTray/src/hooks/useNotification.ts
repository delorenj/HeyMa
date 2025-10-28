/**
 * useNotification hook
 * Provides easy access to notistack for notifications
 */

import { useSnackbar, VariantType } from 'notistack';
import { useCallback } from 'react';

export interface NotificationOptions {
  variant?: VariantType;
  autoHideDuration?: number;
  persist?: boolean;
}

export function useNotification() {
  const { enqueueSnackbar, closeSnackbar } = useSnackbar();

  const showNotification = useCallback(
    (message: string, options: NotificationOptions = {}) => {
      const { variant = 'default', autoHideDuration = 5000, persist = false } = options;

      return enqueueSnackbar(message, {
        variant,
        autoHideDuration: persist ? null : autoHideDuration,
        anchorOrigin: {
          vertical: 'bottom',
          horizontal: 'right',
        },
      });
    },
    [enqueueSnackbar]
  );

  const showSuccess = useCallback(
    (message: string) => showNotification(message, { variant: 'success' }),
    [showNotification]
  );

  const showError = useCallback(
    (message: string) => showNotification(message, { variant: 'error', autoHideDuration: 7000 }),
    [showNotification]
  );

  const showWarning = useCallback(
    (message: string) => showNotification(message, { variant: 'warning' }),
    [showNotification]
  );

  const showInfo = useCallback(
    (message: string) => showNotification(message, { variant: 'info' }),
    [showNotification]
  );

  return {
    showNotification,
    showSuccess,
    showError,
    showWarning,
    showInfo,
    closeNotification: closeSnackbar,
  };
}
