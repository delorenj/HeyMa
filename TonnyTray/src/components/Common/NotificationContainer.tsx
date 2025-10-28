/**
 * Notification container component
 * Displays toast notifications
 */

import { useEffect } from 'react';
import { Snackbar, Alert, IconButton } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { useAppStore } from '@hooks/useTauriState';

export default function NotificationContainer() {
  const { notifications, removeNotification } = useAppStore();

  // Auto-remove notifications after their duration
  useEffect(() => {
    const timers: NodeJS.Timeout[] = [];

    notifications.forEach((notification) => {
      if (notification.duration) {
        const timer = setTimeout(() => {
          removeNotification(notification.id);
        }, notification.duration);
        timers.push(timer);
      }
    });

    return () => {
      timers.forEach((timer) => clearTimeout(timer));
    };
  }, [notifications, removeNotification]);

  return (
    <>
      {notifications.map((notification, index) => (
        <Snackbar
          key={notification.id}
          open={true}
          anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
          sx={{ top: `${80 + index * 80}px !important` }}
        >
          <Alert
            severity={notification.type}
            variant="filled"
            sx={{ width: '100%', minWidth: 300 }}
            action={
              <>
                {notification.action && (
                  <IconButton
                    size="small"
                    color="inherit"
                    onClick={notification.action.callback}
                  >
                    {notification.action.label}
                  </IconButton>
                )}
                <IconButton
                  size="small"
                  color="inherit"
                  onClick={() => removeNotification(notification.id)}
                >
                  <CloseIcon fontSize="small" />
                </IconButton>
              </>
            }
          >
            <strong>{notification.title}</strong>
            {notification.message && (
              <>
                <br />
                {notification.message}
              </>
            )}
          </Alert>
        </Snackbar>
      ))}
    </>
  );
}
