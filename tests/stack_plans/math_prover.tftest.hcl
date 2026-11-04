# The research sample: tasks and a dataset, no bot, with retention and write-once.
mock_provider "aws" {
  source = "./tests/mocks"
}

run "plans_tasks_and_a_dataset_without_a_bot" {
  command = plan

  assert {
    condition     = length(module.image) == 0 && length(module.bot) == 0 && length(module.records) == 0
    error_message = "a research project has no image repository, instance or records bucket"
  }

  assert {
    condition     = length(module.network) == 1 && length(module.tasks) == 1 && length(module.task_results) == 1
    error_message = "a task runner needs a network, a launch template and a results bucket"
  }

  assert {
    condition     = keys(module.datasets) == ["artifacts"] && keys(module.task_profiles) == ["train"]
    error_message = "the sample has the artifacts dataset and the train profile"
  }

  assert {
    condition     = var.datasets[0].versioning && var.datasets[0].expire_prefixes["tmp/"] == 30
    error_message = "the sample's retention must reach the dataset module"
  }

  assert {
    condition     = output.instance_id == null && output.image_repository_url == null
    error_message = "the bot's outputs must be null on a research project"
  }
}
