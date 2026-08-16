return {
  {
    "carderne/pi-nvim",
    version = "v0.2.4",
    config = function()
      require("pi-nvim").setup({
        socket_path = nil,
        set_default_keymaps = false,
      })
    end,
    keys = {
      { "<leader>aa", "<cmd>Pi<cr>", desc = "Pi: send prompt/context", mode = { "n", "x" } },
      { "<leader>af", "<cmd>PiSendFile<cr>", desc = "Pi: send file" },
      { "<leader>as", "<cmd>PiSendSelection<cr>", desc = "Pi: send selection", mode = "x" },
      { "<leader>ab", "<cmd>PiSendBuffer<cr>", desc = "Pi: send buffer" },
      { "<leader>ap", "<cmd>PiPing<cr>", desc = "Pi: ping session" },
      { "<leader>ai", "<cmd>PiSessions<cr>", desc = "Pi: select session" },
    },
  },
  {
    "georgeguimaraes/review.nvim",
    version = "v1.9.1",
    dependencies = {
      { "esmuellert/codediff.nvim", version = "v2.53.0" },
      "MunifTanjim/nui.nvim",
    },
    cmd = { "Review" },
    opts = {
      codediff = {
        readonly = true,
      },
    },
    keys = {
      { "<leader>rr", "<cmd>Review<cr>", desc = "Review: current changes" },
      { "<leader>rc", "<cmd>Review commits<cr>", desc = "Review: commits/range" },
      { "<leader>re", "<cmd>Review export<cr>", desc = "Review: export for Pi" },
      { "<leader>rq", "<cmd>Review close<cr>", desc = "Review: export and close" },
      { "<leader>ar", "<cmd>Review export<cr>", desc = "Pi: export review (then send)" },
    },
  },
}
