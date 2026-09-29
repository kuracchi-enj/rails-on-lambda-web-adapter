import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { ZIP_RUNTIME_ENV } from './runtime-env.ts';

const PROJECT_TAG = 'rails-lwa-verify';

// GetLayerVersionByArn-confirmed 1.1.0 build — see MANIFEST.md "確定した前提値".
const LWA_LAYER_ARM64_ARN =
  'arn:aws:lambda:ap-northeast-1:753240598075:layer:LambdaAdapterLayerArm64:30';

const DB_NAME = 'rlwa';

type AppKey = 'web' | 'api';

export interface RlwaAppStackProps extends cdk.StackProps {
  /** Aurora cluster write endpoint hostname, from `RlwaNetworkDbStack`. */
  readonly clusterEndpointHostname: string;
  /** ARN of the DB master-user secret. */
  readonly dbSecretArn: string;
  /** ARN of the plain SECRET_KEY_BASE secret. */
  readonly secretKeyBaseSecretArn: string;
}

function contextBoolean(value: unknown, defaultValue: boolean): boolean {
  if (value === undefined || value === null || value === '') return defaultValue;
  if (typeof value === 'boolean') return value;
  const normalized = String(value).trim().toLowerCase();
  return !(normalized === 'false' || normalized === '0');
}

function contextNumber(value: unknown, defaultValue: number): number {
  if (value === undefined || value === null || value === '') return defaultValue;
  const n = Number(value);
  return Number.isFinite(n) ? n : defaultValue;
}

export class RlwaAppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: RlwaAppStackProps) {
    super(scope, id, props);
    cdk.Tags.of(this).add('Project', PROJECT_TAG);

    // ── context ────────────────────────────────────────────────────────
    const memory = contextNumber(this.node.tryGetContext('memory'), 1024);

    // Both resolved relative to this file's own directory (infra/lib), NOT
    // process.cwd() — `cdkd synth` may be invoked from anywhere.
    const appsRoot = path.resolve(
      import.meta.dirname,
      (this.node.tryGetContext('appsDir') as string | undefined) ?? '../../apps'
    );
    const zipRoot = path.resolve(
      import.meta.dirname,
      (this.node.tryGetContext('zipDir') as string | undefined) ?? '../../build/zip'
    );

    const bootsnapEnabled = contextBoolean(this.node.tryGetContext('bootsnap'), true);
    const eagerLoadEnabled = contextBoolean(this.node.tryGetContext('eagerLoad'), true);
    const yjitEnabled = contextBoolean(this.node.tryGetContext('yjit'), true);
    // LWA's default is false; the baseline measurement keeps it.
    const asyncInitEnabled = contextBoolean(this.node.tryGetContext('asyncInit'), false);
    // Empty (default): no-op. Non-empty: forces a new version of every
    // URL-bearing function to be published on redeploy (env var change ->
    // new $LATEST -> `currentVersion` changes -> the `live` alias points at
    // a version with zero warm execution environments), so a bench run can
    // force an all-cold batch by passing a fresh value each time. Applied
    // only to the 6 URL functions below, NOT `rlwa-api-migrate` (bench/run_matrix.sh
    // never measures migrate's cold start, and invoking it directly by
    // function name/ARN rather than through a version/alias makes the
    // nonce irrelevant there anyway).
    const coldNonce = (this.node.tryGetContext('coldNonce') as string | undefined) ?? '';
    const coldNonceEnv: Record<string, string> = coldNonce !== '' ? { RLWA_COLD_NONCE: coldNonce } : {};

    const allFunctionNames = [
      'rlwa-web-container',
      'rlwa-api-container',
      'rlwa-web-snapstart',
      'rlwa-api-snapstart',
      'rlwa-web-zip',
      'rlwa-api-zip',
      'rlwa-api-migrate',
    ];
    const targetsContext = this.node.tryGetContext('targets') as string | undefined;
    const targetSet = new Set(
      targetsContext && targetsContext.trim() !== ''
        ? targetsContext.split(',').map((s) => s.trim()).filter((s) => s !== '')
        : allFunctionNames
    );

    // ── shared environment ────────────────────────────────────────────
    // config/lambda_env.rb matches BatchGetSecretValue results by full ARN,
    // so both *_SECRET_ARN values must be real ARNs.
    const commonEnv: Record<string, string> = {
      DB_HOST: props.clusterEndpointHostname,
      DB_NAME,
      DB_SSLMODE: 'verify-full',
      DB_SECRET_ARN: props.dbSecretArn,
      SECRET_KEY_BASE_SECRET_ARN: props.secretKeyBaseSecretArn,
      SECRETS_ARNS: `${props.dbSecretArn},${props.secretKeyBaseSecretArn}`,
      RAILS_LOG_LEVEL: 'info',
    };

    function tuningEnvFor(functionName: string): Record<string, string> {
      if (!targetSet.has(functionName)) return {};
      const env: Record<string, string> = {
        RAILS_EAGER_LOAD: String(eagerLoadEnabled),
        RAILS_YJIT: String(yjitEnabled),
        AWS_LWA_ASYNC_INIT: String(asyncInitEnabled),
      };
      // Only set when OFF: bootsnap itself skips setup when DISABLE_BOOTSNAP
      // is set (Bootsnap.default_setup, called from `require "bootsnap/setup"`
      // in config/boot.rb), so no app-side check is needed. See runtime-env.ts.
      if (!bootsnapEnabled) env.DISABLE_BOOTSNAP = '1';
      return env;
    }

    // ── per-function IAM role ─────────────────────────────────────────
    const secretsResources = [props.dbSecretArn, props.secretKeyBaseSecretArn];

    const buildExecutionRole = (idPrefix: string): iam.Role => {
      const role = new iam.Role(this, `${idPrefix}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'ReadRlwaSecrets',
          actions: ['secretsmanager:GetSecretValue'],
          resources: secretsResources,
        })
      );
      // secretsmanager:BatchGetSecretValue cannot be scoped to specific
      // secret ARNs when called with an explicit SecretIdList (only its
      // Filters-based form supports resource-level scoping some other way);
      // AWS's own IAM policy example for "Read a group of secrets in a
      // batch" grants it on Resource: "*" and scopes the actual secrets via
      // a companion GetSecretValue statement instead — see
      // https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access_iam-policies.html
      // "Example: Permission to retrieve a group of secret values in a
      // batch". Mirrored here; GetSecretValue above still restricts which
      // secrets can actually be individually read.
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'BatchReadSecretsRequiresWildcard',
          actions: ['secretsmanager:BatchGetSecretValue'],
          resources: ['*'],
        })
      );
      return role;
    };

    // ── log groups ─────────────────────────────────────────────────────
    const buildLogGroup = (idPrefix: string, functionName: string): logs.LogGroup =>
      new logs.LogGroup(this, `${idPrefix}LogGroup`, {
        logGroupName: `/aws/lambda/${functionName}`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

    // ── container images (built once per app, reused by container +
    //    snapstart + migrate variants of that app) ──────────────────────
    const buildImageCode = (app: AppKey): lambda.DockerImageCode => {
      const asset = new ec2_assets.DockerImageAsset(this, `${app === 'web' ? 'Web' : 'Api'}Image`, {
        directory: path.join(appsRoot, app),
        file: 'Dockerfile.lambda',
        platform: ec2_assets.Platform.LINUX_ARM64,
      });
      return lambda.DockerImageCode.fromEcr(asset.repository, { tagOrDigest: asset.imageTag });
    };
    const webImageCode = buildImageCode('web');
    const apiImageCode = buildImageCode('api');
    const imageCodeByApp: Record<AppKey, lambda.DockerImageCode> = { web: webImageCode, api: apiImageCode };

    // ── zip Lambda Web Adapter layer (imported once, shared) ──────────
    const lwaLayer = lambda.LayerVersion.fromLayerVersionArn(this, 'LwaLayerArm64', LWA_LAYER_ARM64_ARN);

    const functionUrlOutputs: Record<string, lambda.FunctionUrl> = {};

    const addVersionAliasAndUrl = (idPrefix: string, fn: lambda.Function): void => {
      // SnapStart only applies to a PUBLISHED VERSION + its alias, so every
      // function gets the same version/alias/URL treatment regardless of
      // package type — this keeps cold-start conditions comparable across
      // the matrix (MANIFEST.md "6 本の関数...同じ構成").
      const alias = new lambda.Alias(this, `${idPrefix}LiveAlias`, {
        aliasName: 'live',
        version: fn.currentVersion,
      });
      const fnUrl = alias.addFunctionUrl({
        authType: lambda.FunctionUrlAuthType.NONE,
      });
      functionUrlOutputs[idPrefix] = fnUrl;
    };

    // ── container-image functions (per app) ────────────────────────────
    for (const app of ['web', 'api'] as const) {
      const idPrefix = app === 'web' ? 'WebContainer' : 'ApiContainer';
      const functionName = `rlwa-${app}-container`;
      const fn = new lambda.DockerImageFunction(this, `${idPrefix}Function`, {
        functionName,
        code: imageCodeByApp[app],
        architecture: lambda.Architecture.ARM_64,
        memorySize: memory,
        timeout: cdk.Duration.seconds(30),
        role: buildExecutionRole(idPrefix),
        logGroup: buildLogGroup(idPrefix, functionName),
        loggingFormat: lambda.LoggingFormat.TEXT,
        environment: { ...commonEnv, ...tuningEnvFor(functionName), ...coldNonceEnv },
      });
      addVersionAliasAndUrl(idPrefix, fn);
    }

    // ── container-image + SnapStart functions (per app, same image) ───
    for (const app of ['web', 'api'] as const) {
      const idPrefix = app === 'web' ? 'WebSnapstart' : 'ApiSnapstart';
      const functionName = `rlwa-${app}-snapstart`;
      const fn = new lambda.DockerImageFunction(this, `${idPrefix}Function`, {
        functionName,
        code: imageCodeByApp[app],
        architecture: lambda.Architecture.ARM_64,
        memorySize: memory,
        timeout: cdk.Duration.seconds(30),
        role: buildExecutionRole(idPrefix),
        logGroup: buildLogGroup(idPrefix, functionName),
        loggingFormat: lambda.LoggingFormat.TEXT,
        // Allowed for container-image functions in this CDK version: the L2
        // `configureSnapStart` validation only rejects a runtime-mismatch
        // when `props.runtime !== Runtime.FROM_IMAGE` (see
        // node_modules/aws-cdk-lib/aws-lambda/lib/function.js
        // `configureSnapStart`) — a container image function always has
        // `runtime === FROM_IMAGE`, so the check is a no-op here. LWA (not
        // the Lambda platform) implements the actual before-checkpoint /
        // after-restore hooks — see MANIFEST.md "SnapStart".
        snapStart: lambda.SnapStartConf.ON_PUBLISHED_VERSIONS,
        environment: {
          ...commonEnv,
          ...tuningEnvFor(functionName),
          ...coldNonceEnv,
          AWS_LWA_SNAPSTART_BEFORE_CHECKPOINT_PATH: '/_lwa/before_checkpoint',
          AWS_LWA_SNAPSTART_AFTER_RESTORE_PATH: '/_lwa/after_restore',
        },
      });
      addVersionAliasAndUrl(idPrefix, fn);
    }

    // ── zip functions (per app) ─────────────────────────────────────────
    for (const app of ['web', 'api'] as const) {
      const idPrefix = app === 'web' ? 'WebZip' : 'ApiZip';
      const functionName = `rlwa-${app}-zip`;
      const fn = new lambda.Function(this, `${idPrefix}Function`, {
        functionName,
        runtime: lambda.Runtime.RUBY_3_4,
        handler: 'run.sh',
        code: lambda.Code.fromAsset(path.join(zipRoot, `${app}.zip`)),
        architecture: lambda.Architecture.ARM_64,
        memorySize: memory,
        timeout: cdk.Duration.seconds(30),
        role: buildExecutionRole(idPrefix),
        logGroup: buildLogGroup(idPrefix, functionName),
        loggingFormat: lambda.LoggingFormat.TEXT,
        layers: [lwaLayer],
        environment: {
          ...commonEnv,
          ...ZIP_RUNTIME_ENV,
          ...tuningEnvFor(functionName),
          ...coldNonceEnv,
          AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap',
        },
      });
      addVersionAliasAndUrl(idPrefix, fn);
    }

    // ── migrate function (api container image, no URL) ─────────────────
    {
      const idPrefix = 'ApiMigrate';
      const functionName = 'rlwa-api-migrate';
      new lambda.DockerImageFunction(this, `${idPrefix}Function`, {
        functionName,
        code: apiImageCode,
        architecture: lambda.Architecture.ARM_64,
        memorySize: memory,
        timeout: cdk.Duration.seconds(300),
        role: buildExecutionRole(idPrefix),
        logGroup: buildLogGroup(idPrefix, functionName),
        loggingFormat: lambda.LoggingFormat.TEXT,
        environment: {
          ...commonEnv,
          ...tuningEnvFor(functionName),
          RLWA_ENABLE_TASK_EVENTS: 'true',
          // /_lwa/ is excluded from host authorization and SSL redirect in production.rb
          AWS_LWA_PASS_THROUGH_PATH: '/_lwa/tasks',
        },
      });
      // No Version/Alias/Function URL: invoked directly via
      // `aws lambda invoke --payload '{"task":"db:migrate"}'` (non-HTTP event).
    }

    // ── outputs ──────────────────────────────────────────────────────
    for (const [idPrefix, fnUrl] of Object.entries(functionUrlOutputs)) {
      new cdk.CfnOutput(this, `${idPrefix}FunctionUrl`, {
        value: fnUrl.url,
        description: `Function URL for ${idPrefix}`,
      });
    }
  }
}
