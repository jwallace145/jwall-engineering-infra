# A research project with a dataset and nothing else: no task runner, so no VPC.
mock_provider "aws" {
  source = "./tests/mocks"
}

run "plans_a_dataset_alone" {
  command = plan

  assert {
    condition     = keys(module.datasets) == ["artifacts"]
    error_message = "the project's dataset must be planned"
  }

  assert {
    condition     = length(module.network) == 0 && output.vpc_id == null
    error_message = "a project without a task runner creates no VPC"
  }

  assert {
    condition     = length(module.tasks) == 0 && length(module.task_results) == 0 && length(module.task_profiles) == 0
    error_message = "a project without a task runner has no task resources"
  }

  assert {
    condition     = length(module.image) == 0 && length(module.bot) == 0 && length(module.records) == 0
    error_message = "a research project has no image repository, instance or records bucket"
  }
}
