/**
 * Profile selector component
 * Allows switching between user profiles
 */

import { useState } from 'react';
import {
  Paper,
  Box,
  Avatar,
  Typography,
  Button,
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
  Divider,
} from '@mui/material';
import PersonIcon from '@mui/icons-material/Person';
import ArrowDropDownIcon from '@mui/icons-material/ArrowDropDown';
import AddIcon from '@mui/icons-material/Add';
import CheckIcon from '@mui/icons-material/Check';
import { useProfiles } from '@hooks/useTauriState';

export default function ProfileSelector() {
  const { activeProfile, profiles, switchProfile } = useProfiles();
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);
  const open = Boolean(anchorEl);

  const handleClick = (event: React.MouseEvent<HTMLElement>) => {
    setAnchorEl(event.currentTarget);
  };

  const handleClose = () => {
    setAnchorEl(null);
  };

  const handleSelectProfile = async (profileId: string) => {
    try {
      await switchProfile(profileId);
      handleClose();
    } catch (error) {
      console.error('Failed to switch profile:', error);
    }
  };

  const getPermissionColor = (permission: string) => {
    switch (permission) {
      case 'admin':
        return '#F44336';
      case 'user':
        return '#2196F3';
      case 'kid':
        return '#4CAF50';
      case 'guest':
        return '#9E9E9E';
      default:
        return '#757575';
    }
  };

  return (
    <>
      <Paper sx={{ p: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          <Avatar
            sx={{
              bgcolor: getPermissionColor(activeProfile.permissions),
              width: 48,
              height: 48,
            }}
          >
            {activeProfile.avatar ? (
              <img
                src={activeProfile.avatar}
                alt={activeProfile.name}
                style={{ width: '100%', height: '100%', objectFit: 'cover' }}
              />
            ) : (
              <PersonIcon />
            )}
          </Avatar>

          <Box sx={{ flex: 1 }}>
            <Typography variant="h6">{activeProfile.name}</Typography>
            <Typography variant="body2" color="text.secondary">
              {activeProfile.permissions.charAt(0).toUpperCase() +
                activeProfile.permissions.slice(1)}{' '}
              • {activeProfile.usageStats.totalCommands} commands
            </Typography>
          </Box>

          <Button
            variant="outlined"
            endIcon={<ArrowDropDownIcon />}
            onClick={handleClick}
          >
            Switch Profile
          </Button>
        </Box>
      </Paper>

      <Menu
        anchorEl={anchorEl}
        open={open}
        onClose={handleClose}
        PaperProps={{
          sx: { minWidth: 250 },
        }}
      >
        {profiles.map((profile) => (
          <MenuItem
            key={profile.id}
            onClick={() => handleSelectProfile(profile.id)}
            selected={profile.id === activeProfile.id}
          >
            {profile.id === activeProfile.id && (
              <ListItemIcon>
                <CheckIcon fontSize="small" />
              </ListItemIcon>
            )}
            <Avatar
              sx={{
                bgcolor: getPermissionColor(profile.permissions),
                width: 32,
                height: 32,
                mr: 2,
              }}
            >
              {profile.avatar ? (
                <img
                  src={profile.avatar}
                  alt={profile.name}
                  style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                />
              ) : (
                <PersonIcon fontSize="small" />
              )}
            </Avatar>
            <ListItemText
              primary={profile.name}
              secondary={profile.permissions}
            />
          </MenuItem>
        ))}
        <Divider />
        <MenuItem onClick={handleClose}>
          <ListItemIcon>
            <AddIcon fontSize="small" />
          </ListItemIcon>
          <ListItemText>Add Profile</ListItemText>
        </MenuItem>
      </Menu>
    </>
  );
}
