require_relative "lambda_lifecycle"

Rails.application.routes.draw do
  resources :comments
  resources :posts
  # Define your application routes per the DSL in https://guides.rubyonrails.org/routing.html

  # Lambda SnapStart のライフサイクルフック (config/lambda_lifecycle.rb)。
  # ActionController を経由しない Rack アプリを直接マウントしているため CSRF の影響を受けない。
  # LWA はフック用の環境変数に設定されたパスへの外部リクエストを 403 で遮断するので、
  # その環境変数がある関数（SnapStart 版）でだけマウントする。
  if ENV["AWS_LWA_SNAPSTART_BEFORE_CHECKPOINT_PATH"].present?
    post "_lwa/before_checkpoint" => LambdaLifecycle::BeforeCheckpoint
    post "_lwa/after_restore" => LambdaLifecycle::AfterRestore
  end

  # Reveal health status on /up that returns 200 if the app boots with no exceptions, otherwise 500.
  # Can be used by load balancers and uptime monitors to verify that the app is live.
  get "up" => "rails/health#show", as: :rails_health_check

  # Render dynamic PWA files from app/views/pwa/* (remember to link manifest in application.html.erb)
  # get "manifest" => "rails/pwa#manifest", as: :pwa_manifest
  # get "service-worker" => "rails/pwa#service_worker", as: :pwa_service_worker

  # Defines the root path route ("/")
  root "posts#index"
end
