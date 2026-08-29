return {
  -- diffview for browsing commits and file history
  {
    "sindrets/diffview.nvim",
    cmd = { "DiffviewOpen", "DiffviewClose", "DiffviewFileHistory" },
    keys = {
      { "gd", "<cmd>DiffviewOpen<cr>", desc = "Diff View" },
      { "<leader>gd", "<cmd>DiffviewOpen<cr>", desc = "Diff View" },
      { "gD", "<cmd>DiffviewClose<cr>", desc = "Close Diff View" },
      { "gh", "<cmd>DiffviewFileHistory %<cr>", desc = "File History" },
      { "gH", "<cmd>DiffviewFileHistory<cr>", desc = "Branch History" },
    },
  },
}
