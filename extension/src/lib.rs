use std::path::{Component, Path};

use zed_extension_api as zed;

struct TransferOnSave;

impl zed::Extension for TransferOnSave {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        _server: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<zed::Command> {
        let root = worktree.root_path();
        let command = worktree.which("zed-transfer").ok_or_else(|| {
            "Install zed-transfer with pipx and make it available on Zed's PATH.".to_string()
        })?;
        let executable = Path::new(&command);
        if !executable.is_absolute()
            || executable.starts_with(&root)
            || executable.components().any(|part| part == Component::ParentDir)
        {
            return Err("Zed Transfer requires a client installed outside the project.".into());
        }

        // Preserve SSH agent access and prevent the workspace from extending Python's import path.
        let mut env: Vec<_> = worktree
            .shell_env()
            .into_iter()
            .filter(|(name, _)| name == "SSH_AUTH_SOCK")
            .collect();
        env.extend([
            ("PYTHONPATH".into(), "".into()),
            ("PYTHONHOME".into(), "".into()),
            ("PYTHONNOUSERSITE".into(), "1".into()),
            ("PYTHONSAFEPATH".into(), "1".into()),
        ]);

        Ok(zed::Command {
            command,
            args: vec!["--root".into(), root, "lsp".into()],
            env,
        })
    }
}

zed::register_extension!(TransferOnSave);
