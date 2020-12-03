"""Change id type

Revision ID: 67c1128ba667
Revises: be04415e18e3
Create Date: 2020-12-03 16:54:40.291708

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '67c1128ba667'
down_revision = 'be04415e18e3'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('vk_users', 'current_id',
               existing_type=mysql.INTEGER(),
               type_=sa.String(length=256),
               existing_nullable=True)
    op.alter_column('vk_users', 'found_id',
               existing_type=mysql.INTEGER(),
               type_=sa.String(length=256),
               existing_nullable=True)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('vk_users', 'found_id',
               existing_type=sa.String(length=256),
               type_=mysql.INTEGER(),
               existing_nullable=True)
    op.alter_column('vk_users', 'current_id',
               existing_type=sa.String(length=256),
               type_=mysql.INTEGER(),
               existing_nullable=True)
    # ### end Alembic commands ###
