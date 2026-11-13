"""initial schema

Revision ID: d5158aeb6720
Revises:
Create Date: 2026-11-10 13:29:23.318603
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd5158aeb6720'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """执行结构升级。"""
    op.create_table('agent_execution_record',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='主键ID'),
    sa.Column('task_id', sa.String(length=50), nullable=False, comment='任务ID'),
    sa.Column('user_request', sa.Text(), nullable=False, comment='用户原始请求'),
    sa.Column('trigger_type', sa.String(length=20), nullable=False, comment='触发类型：user/scheduled'),
    sa.Column('tool_name', sa.String(length=100), nullable=False, comment='工具名称'),
    sa.Column('tool_args', sa.Text(), nullable=True, comment='工具参数（JSON序列化）'),
    sa.Column('tool_result_summary', sa.Text(), nullable=True, comment='工具结果摘要'),
    sa.Column('status', sa.String(length=20), nullable=False, comment='执行状态：pending/running/success/failed'),
    sa.Column('execution_time', sa.Integer(), nullable=True, comment='执行耗时（毫秒）'),
    sa.Column('error_message', sa.Text(), nullable=True, comment='错误信息'),
    sa.Column('created_time', sa.DateTime(), nullable=True, comment='记录创建时间'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_task',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='主键ID'),
    sa.Column('task_id', sa.String(length=50), nullable=False, comment='任务ID'),
    sa.Column('trigger_type', sa.String(length=20), nullable=False, comment='触发类型：user/cli/feishu/wecom/scheduled'),
    sa.Column('task_type', sa.String(length=50), nullable=False, comment='任务类型：daily/weekly/monthly/user_query/long_task'),
    sa.Column('user_request', sa.Text(), nullable=False, comment='触发任务的自然语言指令'),
    sa.Column('status', sa.String(length=20), nullable=False, comment='状态：pending/running/completed/failed'),
    sa.Column('result', sa.Text(), nullable=True, comment='任务执行结果摘要'),
    sa.Column('error', sa.Text(), nullable=True, comment='错误信息'),
    sa.Column('channel', sa.String(length=20), nullable=True, comment='来源渠道：cli/feishu/wecom/scheduler'),
    sa.Column('conversation_id', sa.String(length=200), nullable=True, comment='目标会话ID，长任务完成后回发到此会话'),
    sa.Column('user_id', sa.String(length=100), nullable=True, comment='触发任务的用户ID'),
    sa.Column('retry_count', sa.Integer(), nullable=False, comment='重试次数'),
    sa.Column('timeout_seconds', sa.Integer(), nullable=False, comment='单次执行超时秒数'),
    sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
    sa.Column('started_at', sa.DateTime(), nullable=True, comment='开始时间'),
    sa.Column('completed_at', sa.DateTime(), nullable=True, comment='完成时间'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id')
    )
    op.create_table('conversation',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='主键ID'),
    sa.Column('conversation_id', sa.String(length=200), nullable=False, comment='平台会话ID'),
    sa.Column('user_id', sa.String(length=100), nullable=False, comment='平台用户ID'),
    sa.Column('channel', sa.String(length=20), nullable=False, comment='消息渠道：cli/feishu/wecom/scheduler'),
    sa.Column('last_agent_state', sa.JSON(), nullable=True, comment='最近一次Agent状态摘要'),
    sa.Column('meta', sa.JSON(), nullable=True, comment='渠道透传的扩展信息'),
    sa.Column('created_at', sa.DateTime(), nullable=False, comment='创建时间'),
    sa.Column('updated_at', sa.DateTime(), nullable=False, comment='更新时间'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel', 'conversation_id', name='uq_conversation_channel_conversation')
    )
    op.create_table('notification_record',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='主键ID'),
    sa.Column('task_id', sa.String(length=50), nullable=False, comment='关联任务ID'),
    sa.Column('channel', sa.String(length=20), nullable=False, comment='通知渠道：email/wechat/feishu'),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False, comment='幂等键'),
    sa.Column('status', sa.String(length=20), nullable=False, comment='状态：pending/success/failed'),
    sa.Column('chunks', sa.Integer(), nullable=False, comment='总分片数'),
    sa.Column('sent_chunks', sa.Integer(), nullable=False, comment='已成功发送的分片数'),
    sa.Column('retry_count', sa.Integer(), nullable=False, comment='重试次数'),
    sa.Column('max_retries', sa.Integer(), nullable=False, comment='最大重试次数'),
    sa.Column('error_message', sa.Text(), nullable=True, comment='错误信息'),
    sa.Column('meta', sa.JSON(), nullable=True, comment='通知上下文'),
    sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
    sa.Column('updated_at', sa.DateTime(), nullable=True, comment='更新时间'),
    sa.Column('sent_at', sa.DateTime(), nullable=True, comment='完成时间'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_table('policy',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='主键ID'),
    sa.Column('title', sa.String(length=500), nullable=False, comment='政策标题'),
    sa.Column('department', sa.String(length=200), nullable=True, comment='发布部门'),
    sa.Column('government_level', sa.String(length=20), nullable=True, comment='政府级别：国家/省/城市'),
    sa.Column('city', sa.String(length=50), nullable=True, comment='城市（城市级政策时填写）'),
    sa.Column('publish_time', sa.DateTime(), nullable=True, comment='发布时间'),
    sa.Column('url', sa.String(length=1000), nullable=False, comment='原文链接'),
    sa.Column('source_name', sa.String(length=100), nullable=True, comment='来源名称'),
    sa.Column('content', sa.Text(), nullable=True, comment='政策正文内容'),
    sa.Column('summary', sa.String(length=1000), nullable=True, comment='简要摘要'),
    sa.Column('keywords', sa.String(length=500), nullable=True, comment='关键词，逗号分隔'),
    sa.Column('importance_score', sa.Float(), nullable=True, comment='重要程度评分 0-100'),
    sa.Column('created_time', sa.DateTime(), nullable=True, comment='记录创建时间'),
    sa.Column('updated_time', sa.DateTime(), nullable=True, comment='最后更新时间'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url')
    )
    op.create_table('pushed_policy',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False, comment='内容指纹'),
    sa.Column('url', sa.String(length=1000), nullable=True, comment='原文链接'),
    sa.Column('title', sa.String(length=500), nullable=True, comment='政策标题'),
    sa.Column('source_name', sa.String(length=100), nullable=True, comment='来源名称'),
    sa.Column('task_id', sa.String(length=50), nullable=True, comment='首次推送的任务ID'),
    sa.Column('first_pushed_at', sa.DateTime(), nullable=True, comment='首次推送时间'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('fingerprint')
    )
    op.create_table('source_health',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source_name', sa.String(length=100), nullable=False, comment='来源名称'),
    sa.Column('status', sa.String(length=20), nullable=False, comment='ok/empty/degraded/failed'),
    sa.Column('item_count', sa.Integer(), nullable=False, comment='本次抓取条数'),
    sa.Column('newest_publish_time', sa.DateTime(), nullable=True, comment='抓到的最新政策日期'),
    sa.Column('error', sa.Text(), nullable=True, comment='失败原因'),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False, comment='连续失败次数'),
    sa.Column('task_id', sa.String(length=50), nullable=True, comment='关联任务ID'),
    sa.Column('checked_at', sa.DateTime(), nullable=True, comment='检查时间'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('analysis',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('policy_id', sa.Integer(), nullable=False, comment='关联政策ID'),
    sa.Column('summary', sa.Text(), nullable=True, comment='一句话总结'),
    sa.Column('plain_explanation', sa.Text(), nullable=True, comment='政策大白话解释'),
    sa.Column('national_direction', sa.Text(), nullable=True, comment='国家释放的信号'),
    sa.Column('industry_impact', sa.JSON(), nullable=True, comment='影响行业列表（JSON）'),
    sa.Column('career_opportunity', sa.JSON(), nullable=True, comment='未来机会列表（JSON）'),
    sa.Column('personal_advice', sa.JSON(), nullable=True, comment='普通大学生建议列表（JSON）'),
    sa.Column('risk_analysis', sa.Text(), nullable=True, comment='风险分析'),
    sa.Column('trend_score', sa.Float(), nullable=True, comment='趋势评分 0-100'),
    sa.Column('raw_response', sa.Text(), nullable=True, comment='AI原始响应'),
    sa.Column('created_time', sa.DateTime(), nullable=True, comment='分析时间'),
    sa.ForeignKeyConstraint(['policy_id'], ['policy.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('policy_id')
    )
    op.create_table('conversation_message',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='主键ID'),
    sa.Column('conversation_id', sa.Integer(), nullable=False, comment='关联conversation.id'),
    sa.Column('channel', sa.String(length=20), nullable=False, comment='消息渠道：cli/feishu/wecom/scheduler'),
    sa.Column('message_id', sa.String(length=200), nullable=False, comment='平台消息ID或系统生成ID'),
    sa.Column('role', sa.String(length=20), nullable=False, comment='消息角色：user/assistant/system'),
    sa.Column('content', sa.Text(), nullable=False, comment='消息内容'),
    sa.Column('meta', sa.JSON(), nullable=True, comment='消息扩展信息'),
    sa.Column('created_at', sa.DateTime(), nullable=False, comment='创建时间'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel', 'conversation_id', 'message_id', name='uq_conversation_message')
    )


def downgrade() -> None:
    """执行结构回退；生产环境默认不自动调用。"""
    op.drop_table('conversation_message')
    op.drop_table('analysis')
    op.drop_table('source_health')
    op.drop_table('pushed_policy')
    op.drop_table('policy')
    op.drop_table('notification_record')
    op.drop_table('conversation')
    op.drop_table('agent_task')
    op.drop_table('agent_execution_record')
