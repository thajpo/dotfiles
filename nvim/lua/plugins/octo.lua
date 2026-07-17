return {
  {
    "pwntester/octo.nvim",
    cmd = "Octo",
    dependencies = {
      "nvim-lua/plenary.nvim",
      "folke/snacks.nvim",
    },
    opts = {
      picker = "snacks",
      enable_builtin = true,
      file_panel = {
        icons = false,
      },
    },
    keys = {
      { "<leader>gil", "<cmd>Octo issue list<cr>", desc = "List GitHub issues" },
      {
        "<leader>giv",
        function()
          vim.ui.input({ prompt = "GitHub issue number: " }, function(issue)
            if issue and issue:match("^%d+$") then
              vim.cmd("Octo issue edit " .. issue)
            end
          end)
        end,
        desc = "View GitHub issue",
      },
      { "<leader>gpl", "<cmd>Octo pr list<cr>", desc = "List GitHub pull requests" },
    },
  },
}
