from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("experiments/", views.experiment_list, name="experiment_list"),
    path("experiments/new/", views.experiment_edit, name="experiment_new"),
    path("experiments/<int:pk>/", views.experiment_detail, name="experiment_detail"),
    path("experiments/<int:pk>/edit/", views.experiment_edit, name="experiment_edit"),
    path("experiments/<int:pk>/upload/", views.experiment_upload, name="experiment_upload"),
    path("experiments/<int:pk>/plan/", views.experiment_plan, name="experiment_plan"),
    path("experiments/<int:pk>/history/", views.experiment_history, name="experiment_history"),
    path("experiments/<int:pk>/action/<slug:action>/", views.experiment_action, name="experiment_action"),
    path("experiments/<int:pk>/download/<slug:kind>/", views.experiment_download, name="experiment_download"),
    path("runs/", views.run_list, name="run_list"),
    path("runs/new/", views.run_edit, name="run_new"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/edit/", views.run_edit, name="run_edit"),
    path("runs/<int:pk>/action/<slug:action>/", views.run_action, name="run_action"),
    path("runs/<int:pk>/download/<slug:kind>/", views.run_download, name="run_download"),
]
