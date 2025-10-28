/**
 * Quick actions component
 * Shows preset commands for common actions
 */

import { Paper, Box, Typography, Button, Grid } from '@mui/material';
import LightModeIcon from '@mui/icons-material/LightMode';
import DarkModeIcon from '@mui/icons-material/DarkMode';
import MovieIcon from '@mui/icons-material/Movie';
import HomeIcon from '@mui/icons-material/Home';
import { useAppStore } from '@hooks/useTauriState';
import { tauriApi } from '@services/tauri';

export default function QuickActions() {
  const { activeProfile } = useAppStore();

  const defaultActions = [
    {
      id: '1',
      name: 'Good Morning',
      icon: <LightModeIcon />,
      command: 'Good morning, turn on the lights and start the coffee',
      color: '#FF9800',
    },
    {
      id: '2',
      name: 'Movie Time',
      icon: <MovieIcon />,
      command: 'Movie mode, dim the lights and close the blinds',
      color: '#9C27B0',
    },
    {
      id: '3',
      name: "I'm Home",
      icon: <HomeIcon />,
      command: "I'm home, unlock the door and turn on the lights",
      color: '#4CAF50',
    },
    {
      id: '4',
      name: 'Goodnight',
      icon: <DarkModeIcon />,
      command: 'Goodnight, turn off all lights and lock the doors',
      color: '#2196F3',
    },
  ];

  const handleQuickAction = async (command: string) => {
    try {
      await tauriApi.integration.sendCommand(command, activeProfile.id);
    } catch (error) {
      console.error('Quick action failed:', error);
    }
  };

  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" gutterBottom>
        Quick Actions
      </Typography>

      <Grid container spacing={2}>
        {defaultActions.map((action) => (
          <Grid item xs={12} sm={6} md={3} key={action.id}>
            <Button
              variant="outlined"
              fullWidth
              startIcon={action.icon}
              onClick={() => handleQuickAction(action.command)}
              sx={{
                py: 2,
                flexDirection: 'column',
                gap: 1,
                borderColor: action.color,
                color: action.color,
                '&:hover': {
                  borderColor: action.color,
                  bgcolor: `${action.color}10`,
                },
              }}
            >
              <Box sx={{ fontSize: '2rem' }}>{action.icon}</Box>
              <Typography variant="body2">{action.name}</Typography>
            </Button>
          </Grid>
        ))}
      </Grid>
    </Paper>
  );
}
