return {
  "kdheepak/lazygit.nvim",
  lazy = true,
  cmd = {
    "LazyGit",
    "LazyGitConfig",
    "LazyGitCurrentFile",
    "LazyGitFilter",
    "LazyGitFilterCurrentFile",
  },
  dependencies = {
    "nvim-lua/plenary.nvim",
    "folke/snacks.nvim",
  },
  keys = {
    {
      "<leader>gg",
      function()
        vim.cmd.tabnew()
        Snacks.terminal.open({ "lazygit" }, {
          cwd = vim.fn.getcwd(),
          win = {
            position = "current",
          },
        })
      end,
      desc = "LazyGit (full screen)",
    },
  },
}
